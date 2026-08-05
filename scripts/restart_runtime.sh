#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BACKEND_SERVICE="${AI4ALL_BACKEND_SERVICE:-ai4all-weixin-backend}"
PROACTIVE_SERVICE="${AI4ALL_PROACTIVE_SERVICE:-ai4all-weixin-proactive-scheduler}"
MONITOR_TIMER="${AI4ALL_MONITOR_TIMER:-ai4all-monitor-health.timer}"
BACKUP_TIMER="${AI4ALL_BACKUP_TIMER:-ai4all-backup.timer}"
READY_URL="${AI4ALL_READY_URL:-http://127.0.0.1:8180/health/ready}"
MONITOR_SCHEDULERS_VALUE="${MONITOR_SCHEDULERS:-proactive_scheduler:90,dreaming_scheduler:900,world_lifecycle_scheduler:900}"

# node-only(厚节点,如 aliyun2)用户级单元:backend(:8180,直连中心 PG 跑 turn)+ access node(:8190 exec/pull/心跳)。
# 这些是 systemctl --user 单元,无需 sudo;无 nginx / 无系统级 monitor/backup timer / 无 proactive-scheduler。
NODE_BACKEND_SERVICE="${AI4ALL_NODE_BACKEND_SERVICE:-ai4all-weixin-backend}"
NODE_AGENT_SERVICE="${AI4ALL_NODE_AGENT_SERVICE:-ai4all-weixin-node}"
NODE_AGENT_READY_URL="${AI4ALL_NODE_AGENT_READY_URL:-http://127.0.0.1:8190/health/live}"

INSTALL_DEPS=0
RESTART_OPENCLAW=0
SKIP_NGINX=0
DRY_RUN=0
ASSUME_YES=0
WAIT_SECONDS=2

usage() {
  cat <<'USAGE'
Usage: scripts/restart_runtime.sh [options]

Restart the AI4ALL runtime after code has already been pulled.

按 .env 的 AI4ALL_ROLE 自动分流(镜像 app/config.py has_central_role):
  central / standalone → 系统级 sudo systemctl 重启 backend+proactive-scheduler、
                          nginx reload、enable monitor/backup timer、:8180 health。
  node-only(如 aliyun2)→ 用户级 systemctl --user 重启 backend(:8180)+access-node(:8190),
                          无 sudo / 无 nginx / 无系统 timer;健康检查覆盖两端口。
设 AI4ALL_ALLOW_NON_CENTRAL=1 可强制走 central 路径(用于特殊场景)。

Options:
  --install-deps      Run .venv/bin/python -m pip install -r requirements.txt first.
  --restart-openclaw  Restart OpenClaw gateway after backend services.
                      DANGEROUS: reconnects ALL WeChat accounts at once (风控 risk).
                      Requires interactive confirmation, or --yes for automation.
  --skip-nginx        Do not run nginx -t or reload nginx.
  --dry-run           Print commands without executing them.
  --yes, -y           Assume "yes" for the OpenClaw gateway restart confirmation.
  --wait-seconds N    Seconds to wait after service restarts before health checks. Default: 2.
  -h, --help          Show this help.

Environment overrides:
  AI4ALL_BACKEND_SERVICE
  AI4ALL_PROACTIVE_SERVICE
  AI4ALL_MONITOR_TIMER
  AI4ALL_READY_URL
  MONITOR_SCHEDULERS
  AI4ALL_ALLOW_NON_CENTRAL   设为 1 跳过 central 角色守卫(用于非 central 机的特殊场景)。
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-deps)
      INSTALL_DEPS=1
      shift
      ;;
    --restart-openclaw)
      RESTART_OPENCLAW=1
      shift
      ;;
    --skip-nginx)
      SKIP_NGINX=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    --wait-seconds)
      WAIT_SECONDS="${2:?--wait-seconds requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [[ "${DRY_RUN}" == "0" ]]; then
    "$@"
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "required file not found: $1" >&2
    exit 1
  fi
}

require_file ".env"
require_file ".venv/bin/python"
require_file "requirements.txt"

# 按角色分流:central/standalone 走系统级 sudo 路径;node-only 走用户级 systemctl --user 路径。
# 逻辑镜像 app/config.py has_central_role:AI4ALL_ROLE 含 central 或 standalone(默认)即 central。
detect_ai4all_role() {
  # 从 .env 取最后一条未注释的 AI4ALL_ROLE;键缺失则回落 standalone(与 config 默认一致)。
  local line raw
  line="$(grep -E '^[[:space:]]*AI4ALL_ROLE[[:space:]]*=' .env | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    echo "standalone"
    return
  fi
  raw="${line#*=}"            # 取 = 之后
  raw="${raw%%#*}"           # 去行内注释(role 仅含字母/逗号,# 必为注释 —— 见 env 行内注释雷区)
  raw="${raw//[[:space:]]/}" # 去全部空白
  echo "${raw,,}"            # 转小写
}

# RUNTIME_MODE = central | node。AI4ALL_ALLOW_NON_CENTRAL=1 强制 central(特殊场景兜底)。
_role="$(detect_ai4all_role)"
if [[ "${AI4ALL_ALLOW_NON_CENTRAL:-0}" == "1" ]] \
   || [[ ",${_role}," == *",central,"* ]] || [[ ",${_role}," == *",standalone,"* ]]; then
  RUNTIME_MODE="central"
else
  RUNTIME_MODE="node"
fi
echo "runtime mode: ${RUNTIME_MODE} (AI4ALL_ROLE='${_role:-<空>}')"

# 全局 gateway restart = 该机所有微信账号同时重连，风控高危（见 production_runbook 规模化运维红线）。
# 在执行任何重启前就地确认/拦截，避免拒绝时已经重启了 backend/nginx。
# dry-run 不需确认；--yes 跳过；交互终端要求显式输入 yes；非交互且无 --yes 则 fail-fast 拒绝。
if [[ "${RESTART_OPENCLAW}" == "1" && "${DRY_RUN}" == "0" && "${ASSUME_YES}" == "0" ]]; then
  if [[ -t 0 ]]; then
    echo "WARNING: 'openclaw gateway restart' 会让该机所有微信账号同时重连（风控高危）。" >&2
    read -r -p "确认全局重启 OpenClaw gateway？输入 yes 继续： " _confirm
    if [[ "${_confirm}" != "yes" ]]; then
      echo "已取消：未确认 OpenClaw gateway restart（其它重启未执行）。" >&2
      exit 3
    fi
  else
    echo "拒绝在非交互环境下全局重启 OpenClaw gateway：请显式加 --yes。" >&2
    exit 3
  fi
fi

if [[ "${INSTALL_DEPS}" == "1" ]]; then
  run .venv/bin/python -m pip install -r requirements.txt
fi

# ===== node-only(厚节点):用户级单元,无 sudo / 无 nginx / 无系统 timer / 无 proactive-scheduler =====
if [[ "${RUNTIME_MODE}" == "node" ]]; then
  run systemctl --user restart "${NODE_BACKEND_SERVICE}"
  run systemctl --user restart "${NODE_AGENT_SERVICE}"

  if [[ "${RESTART_OPENCLAW}" == "1" ]]; then
    run openclaw gateway restart
  fi

  if [[ "${DRY_RUN}" == "0" ]]; then
    sleep "${WAIT_SECONDS}"
  fi

  run curl -fsS "${READY_URL}"               # 厚节点 backend :8180
  run curl -fsS "${NODE_AGENT_READY_URL}"    # access node :8190/health/live
  run systemctl --user is-active --quiet "${NODE_BACKEND_SERVICE}"
  run systemctl --user is-active --quiet "${NODE_AGENT_SERVICE}"

  echo "restart complete (node)"
  exit 0
fi

# ===== central / standalone:系统级 sudo 路径 =====
if [[ "${SKIP_NGINX}" == "0" ]]; then
  run sudo nginx -t
fi

run sudo systemctl restart "${BACKEND_SERVICE}"
run sudo systemctl restart "${PROACTIVE_SERVICE}"

if [[ "${SKIP_NGINX}" == "0" ]]; then
  run sudo systemctl reload nginx
fi

run sudo systemctl enable --now "${MONITOR_TIMER}"
run sudo systemctl enable --now "${BACKUP_TIMER}"

if [[ "${RESTART_OPENCLAW}" == "1" ]]; then
  run openclaw gateway restart
fi

if [[ "${DRY_RUN}" == "0" ]]; then
  sleep "${WAIT_SECONDS}"
fi

run curl -fsS "${READY_URL}"
run systemctl is-active --quiet "${BACKEND_SERVICE}"
run systemctl is-active --quiet "${PROACTIVE_SERVICE}"
run systemctl is-active --quiet "${MONITOR_TIMER}"
run systemctl is-active --quiet "${BACKUP_TIMER}"
run env "MONITOR_SCHEDULERS=${MONITOR_SCHEDULERS_VALUE}" .venv/bin/python scripts/monitor_health.py --dry-run

echo "restart complete"
