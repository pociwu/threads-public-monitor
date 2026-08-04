#!/usr/bin/env bash
set -Eeuo pipefail

source scripts/env.sh
TAILSCALE_IP="$(read_env_value .env TAILSCALE_IP 100.120.200.116)"
LOGIN_PORT="$(read_env_value .env LOGIN_PORT 6080)"
docker compose stop worker

# Chromium leaves these process-singleton symlinks behind when its container is
# interrupted.  The worker is stopped above, so no Chromium process belonging
# to this project can still be using the shared profile at this point.
rm -f -- \
  browser-profile/SingletonLock \
  browser-profile/SingletonCookie \
  browser-profile/SingletonSocket

docker compose --profile login up -d browser-login

echo "請從 Tailscale 私網開啟：http://${TAILSCALE_IP}:${LOGIN_PORT}/vnc.html?autoconnect=true"
echo "完成 Threads 登入後，按 Enter 關閉登入瀏覽器並恢復 Worker。"
read -r

docker compose --profile login stop browser-login
docker compose start worker
echo "登入工作階段已保存，背景擷取已恢復。"
