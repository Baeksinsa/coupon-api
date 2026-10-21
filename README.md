# coupon-api 배포 리포지토리

선착순 쿠폰 발급 API(FastAPI). ALB 뒤에서 백엔드 A/B 두 대가 동일 구성으로 동작한다.

## 구성

| 항목 | 값 |
|---|---|
| OS | Amazon Linux 2023 |
| 런타임 | Python 3.11 + venv (`/opt/coupon-api/venv`) |
| 서버 | uvicorn, `0.0.0.0:8000`, `--workers 2` |
| 설치 경로 | `/opt/coupon-api` |
| 서비스 | systemd `coupon-api.service` (`Restart=always`) |
| 환경변수 | `/etc/coupon-api.env` (root:root, 600) |
| 상태 저장소 | RDS PostgreSQL 16, ElastiCache(Valkey) over TLS |

애플리케이션은 **상태를 로컬에 두지 않는다.** 발급 성공 여부는 RDS 트랜잭션으로만 결정되고,
Redis는 조회 캐시 용도라 장애 시 DB로 폴백한다. 그래서 동일 구성 서버를 여러 대 붙여도 된다.

## 엔드포인트

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/healthz` | ALB 헬스체크 (DB `SELECT 1`, 실패 시 503) |
| GET | `/api/events` | 이벤트 목록 |
| GET | `/api/events/{id}` | 이벤트 단건 |
| POST | `/api/events/{id}/issue` | 쿠폰 발급 (`{"userId": "..."}`) |
| GET | `/api/users/{id}/coupons` | 사용자 발급 내역 |
| POST | `/api/events/{id}/reset` | 시험용 초기화 (운영 기능 아님) |

## B 서버 설치

```bash
sudo dnf install -y git
git clone https://github.com/<org>/<repo>.git
cd <repo>
sudo cp deploy/coupon-api.env.example /etc/coupon-api.env
sudo chmod 600 /etc/coupon-api.env
sudo vi /etc/coupon-api.env          # A 서버와 동일한 값으로 채운다
sudo bash deploy/bootstrap.sh
```

설치 확인:

```bash
systemctl status coupon-api
curl -s localhost:8000/healthz        # {"status":"ok"}
```

## 사전 조건 (A 서버와 동일하게 맞춰야 하는 것)

1. **IAM 인스턴스 프로파일** — `DB_SECRET_ARN`에 대한 `secretsmanager:GetSecretValue` 권한.
   누락되면 기동 시 `AccessDeniedException`으로 죽는다.
2. **보안 그룹** — B 인스턴스에 A와 같은 SG를 붙이거나, RDS(5432)·ElastiCache(6379) SG의
   인바운드에 B의 SG를 추가한다.
3. **서브넷** — ALB가 바라보는 AZ에 맞춰 A와 다른 AZ에 두면 가용성이 올라간다.
4. **ALB 보안 그룹**에서 백엔드 8000 포트 인바운드 허용.

## 배포 갱신

```bash
cd <repo> && git pull
sudo cp -r app /opt/coupon-api/
sudo chown -R ec2-user:ec2-user /opt/coupon-api/app
sudo systemctl restart coupon-api
```
