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
MONITOR_SCHEDULERS_VALUE="${MONITOR_SCHEDULERS:-proactive_scheduler:90,dreaming_scheduler:900}"

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

仅用于具备 central 能力的机器(.env AI4ALL_ROLE 含 central 或 standalone,
镜像 app/config.py has_central_role)。node-only 机请改用:
  systemctl --user restart ai4all-weixin-node
如确需在非 central 机执行,设 AI4ALL_ALLOW_NON_CENTRAL=1 跳过该守卫。

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

# central 角色守卫:本脚本重启的是中心服务(backend / proactive-scheduler / 系统级 systemctl /
# :8180 health),只适用于具备 central 能力的机器。node-only 机(如 aliyun2)跑的是
# systemctl --user ai4all-weixin-node、端口 8190,无这些 unit,误跑会在 systemctl restart 处失败。
# 逻辑镜像 app/config.py has_central_role:AI4ALL_ROLE 含 central 或 standalone(默认)即放行。
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

if [[ "${AI4ALL_ALLOW_NON_CENTRAL:-0}" != "1" ]]; then
  _role="$(detect_ai4all_role)"
  if [[ ",${_role}," != *",central,"* && ",${_role}," != *",standalone,"* ]]; then
    echo "拒绝:本脚本仅用于具备 central 能力的机器(当前 .env AI4ALL_ROLE='${_role:-<空>}')。" >&2
    echo "node-only 机请改用:systemctl --user restart ai4all-weixin-node" >&2
    echo "如确需在本机执行,设 AI4ALL_ALLOW_NON_CENTRAL=1 跳过该守卫。" >&2
    exit 4
  fi
fi

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
