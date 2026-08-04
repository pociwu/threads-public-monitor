#!/usr/bin/env sh
set -eu

# The login browser and the worker intentionally share one persistent Chromium
# profile. An interrupted container can leave these process-singleton symlinks
# behind even though no Chromium process is still alive. The worker is the sole
# browser owner during normal operation, so remove only those exact stale locks
# before Playwright starts Chromium.
rm -f -- \
  /browser-profile/SingletonLock \
  /browser-profile/SingletonCookie \
  /browser-profile/SingletonSocket

exec python -m app.worker
