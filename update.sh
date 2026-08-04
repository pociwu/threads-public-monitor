#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -n "$(git status --porcelain)" ]]; then
  echo "工作目錄有未提交變更，為避免覆蓋而停止更新。" >&2
  exit 1
fi

previous_ref="$(git rev-parse --verify HEAD)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="backups/threads-monitor-${timestamp}.db"

git fetch --tags --force
latest_tag="$(git tag --list 'v[0-9]*' --sort=-v:refname | head -n1)"
if [[ -z "$latest_tag" ]]; then
  echo "找不到正式版本標籤。" >&2
  exit 1
fi
if [[ "$(git describe --tags --exact-match 2>/dev/null || true)" == "$latest_tag" ]]; then
  echo "目前已是最新版本 ${latest_tag}。"
  exit 0
fi

mkdir -p backups
docker compose stop worker web
if [[ -f data/threads-monitor.db ]]; then
  docker compose run --rm web sqlite3 /data/threads-monitor.db ".backup '/backups/threads-monitor-${timestamp}.db'"
fi

rollback() {
  echo "更新失敗，正在回復 ${previous_ref}。" >&2
  git checkout --detach "$previous_ref"
  if [[ -f "$backup_path" ]]; then cp "$backup_path" data/threads-monitor.db; fi
  docker compose build
  docker compose up -d web worker
}
trap rollback ERR

git checkout --detach "$latest_tag"
docker compose build
docker compose run --rm web alembic upgrade head
docker compose up -d web worker
trap - ERR

echo "已更新至 ${latest_tag}，更新前資料庫備份：${backup_path}"
