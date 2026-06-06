#!/usr/bin/env bash
# Deploy the image-understanding C 段 changes to the LIVE OpenClaw gateway.
#
# What it does (idempotent, with backups):
#   1. Surgically patch the running OpenClaw core dist so before_agent_reply hooks
#      receive the inbound media's absolute local path.
#   2. Sync the ai4all bridge plugin (repo -> installed extension copy).
#   3. Enable IMAGE_UNDERSTANDING_ENABLED in backend .env.
#   4. Restart the backend (system service) and the gateway (user service).
#
# Reversible: every modified runtime file is backed up to *.bak.imageunderstanding
# (created once, kept pristine). Run scripts/rollback_image_understanding.sh to revert.
#
# Run from the repo root on the gateway host (aliyun1):
#   bash scripts/deploy_image_understanding.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="/home/jack/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/get-reply-9dLyvuw9.js"
BRIDGE_SRC="$REPO/openclaw-bridge/index.js"
BRIDGE_DST="/home/jack/.openclaw/extensions/ai4all-openclaw-bridge/index.js"
ENV_FILE="$REPO/.env"
NODE="/home/jack/.openclaw/tools/node-v22.22.0/bin/node"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

echo "==> [1/4] Patch OpenClaw core dist: $DIST"
[ -f "$DIST" ] || { echo "ERROR: dist not found: $DIST"; exit 1; }
[ -f "$DIST.bak.imageunderstanding" ] || cp -p "$DIST" "$DIST.bak.imageunderstanding"

python3 - "$DIST" <<'PY'
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()

if "hookCleanedBody" in src:
    print("    already patched, skipping dist edit")
    sys.exit(0)

target = '\t\t\tconst hookResult = await traceGetReplyPhase("reply.before_agent_reply_hooks", () => hookRunner.runBeforeAgentReply({ cleanedBody }, {'
assert src.count(target) == 1, f"expected exactly 1 hook call match, got {src.count(target)}"

T = "\t\t\t"
insert = (
    f"{T}// ai4all: surface inbound media's absolute local path to before_agent_reply hooks.\n"
    f"{T}let hookCleanedBody = cleanedBody;\n"
    f"{T}if (hasInboundMedia(ctx)) {{\n"
    f"{T}\tconst firstMediaPath = normalizeOptionalString(ctx.MediaPath) || (Array.isArray(ctx.MediaPaths) ? ctx.MediaPaths.map((value) => normalizeOptionalString(value)).find(Boolean) : void 0);\n"
    f"{T}\tif (firstMediaPath) {{\n"
    f"{T}\t\tconst firstMediaType = normalizeOptionalString(ctx.MediaType) || (Array.isArray(ctx.MediaTypes) ? ctx.MediaTypes.map((value) => normalizeOptionalString(value)).find(Boolean) : void 0);\n"
    f"{T}\t\thookCleanedBody = [`[media attached: ${{firstMediaPath}}${{firstMediaType ? ` (${{firstMediaType}})` : \"\"}}]`, cleanedBody].filter(Boolean).join(\"\\n\");\n"
    f"{T}\t}}\n"
    f"{T}}}\n"
)
new_call = '\t\t\tconst hookResult = await traceGetReplyPhase("reply.before_agent_reply_hooks", () => hookRunner.runBeforeAgentReply({ cleanedBody: hookCleanedBody }, {'
open(path, "w", encoding="utf-8").write(src.replace(target, insert + new_call))
print("    dist patched")
PY

"$NODE" --check "$DIST" && echo "    dist syntax OK"

echo "==> [2/4] Sync bridge plugin: $BRIDGE_SRC -> $BRIDGE_DST"
[ -f "$BRIDGE_DST" ] || { echo "ERROR: installed bridge not found: $BRIDGE_DST"; exit 1; }
[ -f "$BRIDGE_DST.bak.imageunderstanding" ] || cp -p "$BRIDGE_DST" "$BRIDGE_DST.bak.imageunderstanding"
cp -p "$BRIDGE_SRC" "$BRIDGE_DST"
"$NODE" --check "$BRIDGE_DST" && echo "    bridge syntax OK"

echo "==> [3/4] Enable IMAGE_UNDERSTANDING_ENABLED in $ENV_FILE"
if grep -q '^IMAGE_UNDERSTANDING_ENABLED=' "$ENV_FILE"; then
  sed -i 's/^IMAGE_UNDERSTANDING_ENABLED=.*/IMAGE_UNDERSTANDING_ENABLED=true/' "$ENV_FILE"
else
  printf '\nIMAGE_UNDERSTANDING_ENABLED=true\n' >> "$ENV_FILE"
fi
grep '^IMAGE_UNDERSTANDING_ENABLED=' "$ENV_FILE" | sed 's/^/    /'
grep -q '^DASHSCOPE_API_KEY=.\+' "$ENV_FILE" && echo "    DASHSCOPE_API_KEY present" || echo "    WARNING: DASHSCOPE_API_KEY empty — VL will fall back!"

echo "==> [4/4] Restart services (brief gateway interruption)"
sudo systemctl restart ai4all-weixin-backend.service
echo "    backend restarted"
systemctl --user restart openclaw-gateway.service
echo "    gateway restarted"

echo
echo "==> Done. Verify with a real WeChat image, then:"
echo "    journalctl -u ai4all-weixin-backend.service -n 50 --no-pager | grep 'openclaw_turn received'"
echo "    # expect: type=image and a non-empty composed content downstream"
