#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "aarch64" ]]; then
  echo "此版本只支援 Ubuntu ARM64（linux/aarch64）。" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 Docker Engine，請先依 Docker 官方文件安裝。" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "找不到 Docker Compose v2。" >&2
  exit 1
fi

mkdir -p data/media backups browser-profile
chmod 700 data backups browser-profile

if [[ ! -f .env ]]; then
  cp .env.example .env
  detected_ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -n "$detected_ip" ]]; then
    sed -i "s/^TAILSCALE_IP=.*/TAILSCALE_IP=${detected_ip}/" .env
  fi
fi

source .env
if ! ip -4 addr show | grep -Fq "$TAILSCALE_IP"; then
  echo "設定的 Tailscale IP ${TAILSCALE_IP} 不在此主機上，請修正 .env。" >&2
  exit 1
fi

docker compose build
docker compose run --rm web alembic upgrade head
docker compose up -d web worker

echo "安裝完成：http://${TAILSCALE_IP}:${WEB_PORT:-8080}"
echo "首次擷取前請執行：bash scripts/login.sh"

