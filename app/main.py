"""선착순 쿠폰 발급 API (PRD v3.0 5절)

- 발급 성공 여부는 PostgreSQL COMMIT으로만 결정한다.
- Redis(Valkey)는 재고 조회 캐시로만 쓰고, 장애 시 DB 조회로 대체한다.
"""
import json
import logging
import os
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

log = logging.getLogger("coupon")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "5"))
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
DEFAULT_EVENTS = [
    (1, "아메리카노 무료 쿠폰", 100),
    (2, "카페라떼 1+1 쿠폰", 50),
    (3, "조각 케이크 50% 할인", 30),
    (4, "영화 관람권 증정", 10),
]
EVENTS_CACHE_KEY = "coupon:events"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    total_qty     INTEGER NOT NULL CHECK (total_qty >= 0),
    remaining_qty INTEGER NOT NULL CHECK (remaining_qty >= 0)
);
CREATE TABLE IF NOT EXISTS coupon_issues (
    id        BIGSERIAL   PRIMARY KEY,
    event_id  INTEGER     NOT NULL REFERENCES events(id),
    user_id   TEXT        NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, user_id)
);
"""


def db_password() -> str:
    """DB_SECRET_ARN(Secrets Manager)을 우선 사용하고, 없으면 DB_PASSWORD를 쓴다."""
    secret_arn = os.getenv("DB_SECRET_ARN")
    if secret_arn:
        import boto3

        client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "ap-northeast-2"))
        return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])["password"]
    return os.environ["DB_PASSWORD"]


def build_conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "coupon"),
        user=os.getenv("DB_USER", "coupon"),
        password=db_password(),
        sslmode=os.getenv("DB_SSLMODE", "require"),
        connect_timeout=5,
    )


def cache_key(event_id: int) -> str:
    return f"coupon:{event_id}:remaining"


class State:
    pool: ConnectionPool
    cache: redis.Redis | None = None


state = State()


def init_db() -> None:
    with state.pool.connection() as conn:
        conn.execute(SCHEMA)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO events (id, name, total_qty, remaining_qty) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (id) DO NOTHING",
                [(event_id, name, qty, qty) for event_id, name, qty in DEFAULT_EVENTS],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.pool = ConnectionPool(
        build_conninfo(),
        min_size=2,
        max_size=int(os.getenv("DB_POOL_SIZE", "10")),
        kwargs={"row_factory": dict_row},
        open=True,
    )
    init_db()
    redis_url = os.getenv("REDIS_URL")
    if CACHE_ENABLED and redis_url:
        state.cache = redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True)
    yield
    state.pool.close()


app = FastAPI(title="coupon-api", lifespan=lifespan)


def cache_get(key: str):
    if state.cache is None:
        return None
    try:
        raw = state.cache.get(key)
        return json.loads(raw) if raw else None
    except redis.RedisError as e:
        log.warning("cache get failed: %s", e)
        return None


def cache_set(key: str, value) -> None:
    if state.cache is None:
        return
    try:
        state.cache.set(key, json.dumps(value, ensure_ascii=False), ex=CACHE_TTL_SECONDS)
    except redis.RedisError as e:
        log.warning("cache set failed: %s", e)


def cache_invalidate(event_id: int) -> None:
    """발급·초기화 후 해당 쿠폰과 전체 목록 캐시를 함께 지운다."""
    if state.cache is None:
        return
    try:
        state.cache.delete(cache_key(event_id), EVENTS_CACHE_KEY)
    except redis.RedisError as e:
        log.warning("cache delete failed: %s", e)


@app.get("/healthz")
def healthz():
    try:
        with state.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001 - 헬스 체크는 어떤 실패든 503
        log.error("health check failed: %s", e)
        return JSONResponse({"status": "db unavailable"}, status_code=503)


@app.get("/api/events")
def list_events():
    cached = cache_get(EVENTS_CACHE_KEY)
    if cached:
        return JSONResponse(cached, headers={"X-Cache": "HIT"})

    with state.pool.connection() as conn:
        events = conn.execute("SELECT id, name, total_qty, remaining_qty FROM events ORDER BY id").fetchall()
    cache_set(EVENTS_CACHE_KEY, events)
    return JSONResponse(events, headers={"X-Cache": "MISS" if state.cache else "OFF"})


@app.get("/api/events/{event_id}")
def get_event(event_id: int):
    cached = cache_get(cache_key(event_id))
    if cached:
        return JSONResponse(cached, headers={"X-Cache": "HIT"})

    with state.pool.connection() as conn:
        event = conn.execute(
            "SELECT id, name, total_qty, remaining_qty FROM events WHERE id = %s", (event_id,)
        ).fetchone()
    if event is None:
        return JSONResponse({"detail": "event not found"}, status_code=404)
    cache_set(cache_key(event_id), event)
    return JSONResponse(event, headers={"X-Cache": "MISS" if state.cache else "OFF"})


class IssueRequest(BaseModel):
    userId: str = Field(min_length=1, max_length=64)


@app.post("/api/events/{event_id}/issue")
def issue_coupon(event_id: int, req: IssueRequest):
    """PRD 5.3: 이벤트 행을 FOR UPDATE로 잠가 재고 판단을 직렬화한다."""
    try:
        with state.pool.connection() as conn, conn.transaction():
            event = conn.execute(
                "SELECT remaining_qty FROM events WHERE id = %s FOR UPDATE", (event_id,)
            ).fetchone()
            if event is None:
                return JSONResponse({"detail": "event not found"}, status_code=404)

            already = conn.execute(
                "SELECT 1 FROM coupon_issues WHERE event_id = %s AND user_id = %s", (event_id, req.userId)
            ).fetchone()
            if already:
                return JSONResponse({"result": "duplicate"}, status_code=409)

            if event["remaining_qty"] <= 0:
                return JSONResponse({"result": "sold_out"}, status_code=410)

            conn.execute("UPDATE events SET remaining_qty = remaining_qty - 1 WHERE id = %s", (event_id,))
            issued = conn.execute(
                "INSERT INTO coupon_issues (event_id, user_id) VALUES (%s, %s) RETURNING id, issued_at",
                (event_id, req.userId),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        # 트랜잭션은 롤백되고, UNIQUE 제약이 중복을 한 번 더 막는다
        return JSONResponse({"result": "duplicate"}, status_code=409)

    cache_invalidate(event_id)
    return JSONResponse(
        {"result": "issued", "couponId": issued["id"], "issuedAt": issued["issued_at"].isoformat()},
        status_code=201,
    )


@app.get("/api/users/{user_id}/coupons")
def user_coupons(user_id: str):
    with state.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.id, c.event_id, e.name, c.issued_at FROM coupon_issues c"
            " JOIN events e ON e.id = c.event_id WHERE c.user_id = %s ORDER BY c.issued_at",
            (user_id,),
        ).fetchall()
    return [{**r, "issued_at": r["issued_at"].isoformat()} for r in rows]


@app.post("/api/events/{event_id}/reset")
def reset_event(event_id: int):
    """시험 환경 초기화(PRD 7.2: 테스트마다 사전 초기화). 운영 기능이 아니다."""
    with state.pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM coupon_issues WHERE event_id = %s", (event_id,))
        updated = conn.execute(
            "UPDATE events SET remaining_qty = total_qty WHERE id = %s RETURNING total_qty", (event_id,)
        ).fetchone()
    if updated is None:
        return JSONResponse({"detail": "event not found"}, status_code=404)
    cache_invalidate(event_id)
    return {"result": "reset", "remaining_qty": updated["total_qty"]}
