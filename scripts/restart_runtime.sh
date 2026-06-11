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
