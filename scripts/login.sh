#!/usr/bin/env bash
set -Eeuo pipefail

source .env
docker compose stop worker
docker compose --profile login up -d browser-login

echo "請從 Tailscale 私網開啟：http://${TAILSCALE_IP}:${LOGIN_PORT:-6080}/vnc.html?autoconnect=true"
echo "完成 Threads 登入後，按 Enter 關閉登入瀏覽器並恢復 Worker。"
read -r

docker compose --profile login stop browser-login
docker compose start worker
echo "登入工作階段已保存，背景擷取已恢復。"

