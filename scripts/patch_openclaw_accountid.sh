#!/usr/bin/env bash
# Durably (re-)apply the OpenClaw core "before_agent_reply hook ctx accountId" patch.
#
# WHY: OpenClaw 6.x core assembles the before_agent_reply hook ctx WITHOUT the bot's
# AccountId. The ai4all bridge reads that ctx to derive `channel_account_id`; without
# AccountId it falls back to the bare channel name ("openclaw-weixin"), so the central
# brain resolves no binding and silently drops the reply. This patch surfaces the bot
# AccountId into the hook ctx so the bridge forwards the correct per-account identity.
# (Root cause + E2E evidence: docs/tech_design/multi_node_weixin_login_investigation_20260614.md §10.)
#
# Idempotent + reversible: skips if already patched; backs up to *.bak.accountid once.
# Host-agnostic: auto-discovers the node binary and the hashed core bundle (the bundle
# name changes every OpenClaw release), so it survives upgrades/reinstalls. Run it on
# ANY host whose OpenClaw gateway processes inbound channel messages (today: aliyun2 the
# access node; also safe on aliyun1 if it is ever upgraded to a 6.x core with this bug).
#
# Usage:
#   bash scripts/patch_openclaw_accountid.sh            # patch + restart gateway
#   bash scripts/patch_openclaw_accountid.sh --no-restart   # patch only (caller restarts)
#
# Overrides (env): OPENCLAW_NODE, OPENCLAW_CORE_DIST_DIR, OPENCLAW_DIST (explicit bundle).
set -euo pipefail

RESTART_GATEWAY=1
for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART_GATEWAY=0 ;;
    *) echo "ERROR: unknown arg: $arg" >&2; exit 2 ;;
  esac
done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# --- Resolve node binary: env override > PATH > openclaw bundled toolchain. ---
NODE="${OPENCLAW_NODE:-}"
[ -n "$NODE" ] || NODE="$(command -v node || true)"
[ -n "$NODE" ] || NODE="$(ls -d /home/jack/.openclaw/tools/node-v*/bin/node 2>/dev/null | head -1 || true)"
[ -n "$NODE" ] && [ -x "$NODE" ] || { echo "ERROR: node not found (set OPENCLAW_NODE)"; exit 1; }

# --- Resolve the target bundle: explicit OPENCLAW_DIST > discover in core dist dir. ---
DIST="${OPENCLAW_DIST:-}"
if [ -z "$DIST" ]; then
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
  [ -d "$CORE_DIST_DIR" ] || { echo "ERROR: openclaw core dist dir not found (set OPENCLAW_CORE_DIST_DIR or OPENCLAW_DIST)"; exit 1; }
  # The hook ctx lives in the same bundle as the before_agent_reply call.
  DIST="$(grep -rl 'runBeforeAgentReply(' "$CORE_DIST_DIR"/get-reply-*.js 2>/dev/null | head -1 || true)"
fi
[ -n "$DIST" ] && [ -f "$DIST" ] || { echo "ERROR: before_agent_reply bundle not found (set OPENCLAW_DIST)"; exit 1; }

echo "==> NODE=$NODE"
echo "==> DIST=$DIST"

[ -f "$DIST.bak.accountid" ] || cp -p "$DIST" "$DIST.bak.accountid"

python3 - "$DIST" <<'PY'
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()

if "accountId: sessionCtx.AccountId" in src:
    print("    already patched, skipping dist edit")
    sys.exit(0)

# Anchor: the hook-ctx object literal's `trigger:` line, immediately followed by the
# channel-fields spread. Insert the accountId field between them. Whitespace (4 tabs)
# matches the readable (non-minified) core bundle on both 5.28 and 6.x.
anchor = '\t\t\t\ttrigger: opts?.isHeartbeat ? "heartbeat" : "user",\n\t\t\t\t...buildAgentHookContextChannelFields({'
n = src.count(anchor)
assert n == 1, f"expected exactly 1 hook-ctx anchor match, got {n} (core layout changed; patch by hand)"

patched = (
    '\t\t\t\ttrigger: opts?.isHeartbeat ? "heartbeat" : "user",\n'
    '\t\t\t\taccountId: sessionCtx.AccountId ?? ctx.AccountId,\n'
    '\t\t\t\t...buildAgentHookContextChannelFields({'
)
open(path, "w", encoding="utf-8").write(src.replace(anchor, patched))
print("    dist patched (accountId hook ctx)")
PY

"$NODE" --check "$DIST" && echo "    dist syntax OK"

if [ "$RESTART_GATEWAY" -eq 1 ]; then
  echo "==> Restart openclaw-gateway.service (brief interruption)"
  systemctl --user restart openclaw-gateway.service
  echo "    gateway restarted"
else
  echo "==> --no-restart: leaving gateway running (caller will restart)"
fi

echo "==> Done."
