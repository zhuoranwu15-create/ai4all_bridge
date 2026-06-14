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
# Run from the repo root on the CENTRAL backend host (the machine that runs both
# the OpenClaw gateway and ai4all-weixin-backend; today that is aliyun1). The core
# dist path, node binary and bridge dest are auto-discovered so this works across
# different OpenClaw install layouts (official installer's bundled node vs `npm i -g`)
# and survives OpenClaw upgrades (the hashed bundle name changes every release).
#
# Override any of these via env if auto-discovery picks the wrong one:
#   OPENCLAW_NODE           path to the node binary
#   OPENCLAW_CORE_DIST_DIR  openclaw core `dist/` directory
#   OPENCLAW_BRIDGE_DST     installed bridge extension index.js
#
#   bash scripts/deploy_image_understanding.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_SRC="$REPO/openclaw-bridge/index.js"
BRIDGE_DST="${OPENCLAW_BRIDGE_DST:-/home/jack/.openclaw/extensions/ai4all-openclaw-bridge/index.js}"
ENV_FILE="$REPO/.env"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# --- Resolve node binary: env override > PATH > openclaw bundled toolchain. ---
NODE="${OPENCLAW_NODE:-}"
[ -n "$NODE" ] || NODE="$(command -v node || true)"
[ -n "$NODE" ] || NODE="$(ls -d /home/jack/.openclaw/tools/node-v*/bin/node 2>/dev/null | head -1 || true)"
[ -n "$NODE" ] && [ -x "$NODE" ] || { echo "ERROR: node not found (set OPENCLAW_NODE)"; exit 1; }

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
[ -d "$CORE_DIST_DIR" ] || { echo "ERROR: openclaw core dist dir not found (set OPENCLAW_CORE_DIST_DIR)"; exit 1; }

# --- Discover the hashed bundle holding the before_agent_reply hook call, by
# --- content (matches both unpatched `{ cleanedBody }` and patched
# --- `{ cleanedBody: hookCleanedBody }`), instead of a hardcoded hash name. ---
DIST="$(grep -rl 'runBeforeAgentReply({ cleanedBody' "$CORE_DIST_DIR"/get-reply-*.js 2>/dev/null | head -1 || true)"
[ -n "$DIST" ] && [ -f "$DIST" ] || { echo "ERROR: before_agent_reply bundle not found in $CORE_DIST_DIR"; exit 1; }

echo "==> Resolved runtime paths:"
echo "    NODE=$NODE"
echo "    CORE_DIST_DIR=$CORE_DIST_DIR"
echo "    DIST=$DIST"
echo "    BRIDGE_DST=$BRIDGE_DST"

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

echo "==> [1b/4] Patch OpenClaw core hook ctx accountId (same bundle, idempotent)"
# Folded in so this script is the single source of truth for all core patches.
# --no-restart: the gateway is restarted once at step [4/4] below. Reuses the same
# discovered $DIST; the helper backs up to *.bak.accountid and skips if already applied.
OPENCLAW_NODE="$NODE" OPENCLAW_DIST="$DIST" bash "$REPO/scripts/patch_openclaw_accountid.sh" --no-restart

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
