#!/usr/bin/env sh
set -eu

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
export DISPLAY=:99
x11vnc -display :99 -forever -shared -nopw -listen 127.0.0.1 -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &

chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --user-data-dir=/browser-profile \
  --window-size=1280,900 \
  --no-first-run \
  --disable-default-apps \
  https://www.threads.com/ &

wait

