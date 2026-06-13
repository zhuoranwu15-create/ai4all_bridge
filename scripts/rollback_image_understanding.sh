#!/usr/bin/env bash
# Revert the image-understanding C 段 live deployment by restoring the pristine
# backups created by deploy_image_understanding.sh, then restarting services.
# Backend code (A 段) is left in place; only the runtime gateway/dist/bridge and
# the IMAGE_UNDERSTANDING_ENABLED flag are reverted.
# Path resolution mirrors deploy_image_understanding.sh; same env overrides apply
# (OPENCLAW_CORE_DIST_DIR, OPENCLAW_BRIDGE_DST). The core dist file is located via
# its pristine *.bak.imageunderstanding backup so the hashed bundle name is irrelevant.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DST="${OPENCLAW_BRIDGE_DST:-/home/jack/.openclaw/extensions/ai4all-openclaw-bridge/index.js}"
ENV_FILE="$REPO/.env"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# --- Resolve openclaw core dist dir: env override > known install layouts. ---
CORE_DIST_DIR="${OPENCLAW_CORE_DIST_DIR:-}"
if [ -z "$CORE_DIST_DIR" ]; then
  for cand in \
    /home/jack/.openclaw/tools/node-v*/lib/node_modules/openclaw/dist \
    /home/jack/.npm-global/lib/node_modules/openclaw/dist \
    "$(npm config get prefix 2>/dev/null)/lib/node_modules/openclaw/dist"; do
    for d in $cand; do
      [ -d "$d" ] && { CORE_DIST_DIR="$d"; break 2; }
    done
  done
fi

echo "==> Restore OpenClaw core dist"
# Locate the live bundle via its pristine backup (hashed name varies by release).
BAK="$([ -n "$CORE_DIST_DIR" ] && ls "$CORE_DIST_DIR"/get-reply-*.js.bak.imageunderstanding 2>/dev/null | head -1 || true)"
if [ -n "$BAK" ] && [ -f "$BAK" ]; then
  DIST="${BAK%.bak.imageunderstanding}"
  cp -p "$BAK" "$DIST"; echo "    restored $DIST"
else
  echo "    no backup found, skipping"
fi

echo "==> Restore bridge plugin"
if [ -f "$BRIDGE_DST.bak.imageunderstanding" ]; then
  cp -p "$BRIDGE_DST.bak.imageunderstanding" "$BRIDGE_DST"; echo "    restored"
else
  echo "    no backup found, skipping"
fi

echo "==> Disable IMAGE_UNDERSTANDING_ENABLED in .env"
sed -i 's/^IMAGE_UNDERSTANDING_ENABLED=.*/IMAGE_UNDERSTANDING_ENABLED=false/' "$ENV_FILE" || true

echo "==> Restart services"
sudo systemctl restart ai4all-weixin-backend.service
systemctl --user restart openclaw-gateway.service
echo "==> Rollback done."
