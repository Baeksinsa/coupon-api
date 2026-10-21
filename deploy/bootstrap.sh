#!/usr/bin/env bash
# 신규 EC2(백엔드 B 서버)에 coupon-api를 서버 A와 동일하게 설치한다.
# 사용법:  sudo bash deploy/bootstrap.sh
set -euo pipefail

APP_DIR=/opt/coupon-api
ENV_FILE=/etc/coupon-api.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER=ec2-user

if [[ $EUID -ne 0 ]]; then
  echo "root 권한이 필요합니다: sudo bash deploy/bootstrap.sh" >&2
  exit 1
fi

echo "==> 1/6 패키지 설치 (python3.11, postgresql16 클라이언트)"
dnf install -y python3.11 python3.11-pip postgresql16 >/dev/null

echo "==> 2/6 애플리케이션 복사 -> ${APP_DIR}"
mkdir -p "${APP_DIR}"
cp -r "${SRC_DIR}/app" "${APP_DIR}/"
cp "${SRC_DIR}/requirements.txt" "${APP_DIR}/"
cp "${SRC_DIR}/deploy/coupon-api.service" "${APP_DIR}/"
chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"

echo "==> 3/6 venv 생성 및 의존성 설치"
sudo -u "${RUN_USER}" python3.11 -m venv "${APP_DIR}/venv"
sudo -u "${RUN_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip >/dev/null
sudo -u "${RUN_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> 4/6 환경변수 파일 확인 (${ENV_FILE})"
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${SRC_DIR}/deploy/coupon-api.env.example" "${ENV_FILE}"
  chown root:root "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "    !! ${ENV_FILE} 를 템플릿으로 생성했습니다. DB_HOST / DB_SECRET_ARN / REDIS_URL 을"
  echo "       서버 A와 동일한 값으로 채운 뒤 다시 실행하거나 서비스를 재시작하세요."
  NEED_ENV_EDIT=1
else
  chmod 600 "${ENV_FILE}"
  NEED_ENV_EDIT=0
fi

echo "==> 5/6 systemd 등록"
install -m 644 "${SRC_DIR}/deploy/coupon-api.service" /etc/systemd/system/coupon-api.service
systemctl daemon-reload
systemctl enable coupon-api

if [[ "${NEED_ENV_EDIT}" == "1" ]]; then
  echo "==> 6/6 환경변수 미설정으로 기동 생략. 값 입력 후:  sudo systemctl start coupon-api"
  exit 0
fi

echo "==> 6/6 서비스 기동 및 헬스체크"
systemctl restart coupon-api
for i in $(seq 1 15); do
  if curl -fsS -m 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "    OK: $(curl -s http://127.0.0.1:8000/healthz)"
    exit 0
  fi
  sleep 2
done

echo "    실패: 헬스체크 응답 없음. 로그 확인 -> journalctl -u coupon-api -n 50 --no-pager" >&2
exit 1
