#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BACKEND_SERVICE="${AI4ALL_BACKEND_SERVICE:-ai4all-weixin-backend}"
PROACTIVE_SERVICE="${AI4ALL_PROACTIVE_SERVICE:-ai4all-weixin-proactive-scheduler}"
MONITOR_TIMER="${AI4ALL_MONITOR_TIMER:-ai4all-monitor-health.timer}"
READY_URL="${AI4ALL_READY_URL:-http://127.0.0.1:8180/health/ready}"
MONITOR_SCHEDULERS_VALUE="${MONITOR_SCHEDULERS:-proactive_scheduler:90,dreaming_scheduler:900}"

INSTALL_DEPS=0
RESTART_OPENCLAW=0
SKIP_NGINX=0
DRY_RUN=0
WAIT_SECONDS=2

usage() {
  cat <<'USAGE'
Usage: scripts/restart_runtime.sh [options]

Restart the AI4ALL runtime after code has already been pulled.

Options:
  --install-deps      Run .venv/bin/python -m pip install -r requirements.txt first.
  --restart-openclaw  Restart OpenClaw gateway after backend services.
  --skip-nginx        Do not run nginx -t or reload nginx.
  --dry-run           Print commands without executing them.
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
run env "MONITOR_SCHEDULERS=${MONITOR_SCHEDULERS_VALUE}" .venv/bin/python scripts/monitor_health.py --dry-run

echo "restart complete"
