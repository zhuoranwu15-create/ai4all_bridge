#!/usr/bin/env bash
# Revert the image-understanding C 段 live deployment by restoring the pristine
# backups created by deploy_image_understanding.sh, then restarting services.
# Backend code (A 段) is left in place; only the runtime gateway/dist/bridge and
# the IMAGE_UNDERSTANDING_ENABLED flag are reverted.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="/home/jack/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/get-reply-9dLyvuw9.js"
BRIDGE_DST="/home/jack/.openclaw/extensions/ai4all-openclaw-bridge/index.js"
ENV_FILE="$REPO/.env"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

echo "==> Restore OpenClaw core dist"
if [ -f "$DIST.bak.imageunderstanding" ]; then
  cp -p "$DIST.bak.imageunderstanding" "$DIST"; echo "    restored"
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
