#!/usr/bin/env sh
set -eu

rm -f /tmp/.X99-lock
export DISPLAY=:99
Xtigervnc :99 \
  -geometry 1280x900 \
  -depth 24 \
  -rfbport 5900 \
  -SecurityTypes None \
  -AlwaysShared=1 \
  -DisconnectClients=0 \
  -nolisten tcp \
  >/tmp/xvnc.log 2>&1 &

attempt=0
until xdpyinfo -display :99 >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    echo "Xtigervnc did not become ready" >&2
    cat /tmp/xvnc.log >&2
    exit 1
  fi
  sleep 0.2
done

fluxbox -display :99 >/tmp/fluxbox.log 2>&1 &
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &

exec chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-gpu-compositing \
  --ozone-platform=x11 \
  --password-store=basic \
  --user-data-dir=/browser-profile \
  --window-position=0,0 \
  --window-size=1280,900 \
  --no-first-run \
  --disable-default-apps \
  https://www.threads.com/
