#!/usr/bin/env sh
set -eu

rm -f /tmp/.X99-lock
export DISPLAY=:99
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &

attempt=0
until xdpyinfo -display :99 >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    echo "Xvfb did not become ready" >&2
    cat /tmp/xvfb.log >&2
    exit 1
  fi
  sleep 0.2
done

fluxbox -display :99 >/tmp/fluxbox.log 2>&1 &
x11vnc \
  -display :99 \
  -forever \
  -shared \
  -nopw \
  -listen 127.0.0.1 \
  -rfbport 5900 \
  -noxdamage \
  -nowf \
  -noscr \
  -fixscreen V=3.0 \
  >/tmp/x11vnc.log 2>&1 &
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
