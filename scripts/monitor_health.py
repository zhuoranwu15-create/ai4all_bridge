import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import get_scheduler_heartbeat, init_db  # noqa: E402

DEFAULT_STATE_FILE = "/tmp/ai4all_monitor_health_state.json"


def _http_get_json(url: str, timeout: float) -> Tuple[bool, Dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return 200 <= response.status < 300, data
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {"body": body[:500]}
        return False, {"status_code": err.code, "response": data}
    except Exception as err:
        return False, {"error": str(err)}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _parse_scheduler_specs(values: List[str]) -> List[Tuple[str, int]]:
    specs: List[Tuple[str, int]] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" not in item:
                raise ValueError(f"invalid scheduler spec: {item}")
            service, max_age = item.split(":", 1)
            specs.append((service.strip(), int(max_age.strip())))
    return specs


def _check_scheduler(service: str, max_age_seconds: int) -> Optional[str]:
    heartbeat = get_scheduler_heartbeat(service)
    if heartbeat is None:
        return f"{service}: heartbeat missing"
    last_seen_at = _parse_timestamp(heartbeat.get("last_seen_at"))
    if last_seen_at is None:
        return f"{service}: invalid last_seen_at={heartbeat.get('last_seen_at')}"
    age_seconds = int((datetime.now() - last_seen_at).total_seconds())
    if age_seconds > max_age_seconds:
        return f"{service}: heartbeat stale age={age_seconds}s max={max_age_seconds}s"
    if heartbeat.get("status") in {"error", "failed"}:
        return f"{service}: last status={heartbeat.get('status')} error={heartbeat.get('last_error') or ''}".strip()
    return None


def _send_feishu_alert(webhook_url: str, text: str, timeout: float) -> None:
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def _load_state(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(path: str, state: Dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def _seconds_since(value: Optional[str]) -> Optional[int]:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return int((datetime.now() - parsed).total_seconds())


def _should_send_failure_alert(
    *,
    state: Dict,
    errors: List[str],
    consecutive_failures: int,
    repeat_after_seconds: int,
) -> bool:
    failure_count = int(state.get("failure_count") or 0) + 1
    state["failure_count"] = failure_count
    state["last_status"] = "error"
    state["last_error_signature"] = "\n".join(errors)
    state["last_failed_at"] = datetime.now().isoformat(timespec="seconds")
    if failure_count < consecutive_failures:
        return False
    if failure_count == consecutive_failures:
        state["last_alert_at"] = datetime.now().isoformat(timespec="seconds")
        return True
    elapsed = _seconds_since(state.get("last_alert_at"))
    if elapsed is None or elapsed >= repeat_after_seconds:
        state["last_alert_at"] = datetime.now().isoformat(timespec="seconds")
        return True
    return False


def _should_send_recovery_alert(*, state: Dict, consecutive_failures: int) -> bool:
    previous_failures = int(state.get("failure_count") or 0)
    previous_alerted = previous_failures >= consecutive_failures and bool(state.get("last_alert_at"))
    state["failure_count"] = 0
    state["last_status"] = "ok"
    state["last_recovered_at"] = datetime.now().isoformat(timespec="seconds")
    return previous_alerted


def main() -> int:
    parser = argparse.ArgumentParser(description="AI4ALL lightweight production health monitor")
    parser.add_argument(
        "--url",
        default=os.getenv("MONITOR_READY_URL", "http://127.0.0.1:8180/health/ready"),
        help="ready endpoint to check",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--scheduler",
        action="append",
        default=[],
        help="scheduler heartbeat check in service:max_age_seconds form",
    )
    parser.add_argument(
        "--webhook-url",
        default=getattr(settings, "feishu_alert_webhook_url", ""),
        help="Feishu alert webhook URL; defaults to FEISHU_ALERT_WEBHOOK_URL",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("MONITOR_STATE_FILE", DEFAULT_STATE_FILE),
        help="local state file used for consecutive-failure alerting",
    )
    parser.add_argument(
        "--consecutive-failures",
        type=int,
        default=int(os.getenv("MONITOR_CONSECUTIVE_FAILURES", "2")),
        help="send alert only after this many consecutive failed checks",
    )
    parser.add_argument(
        "--repeat-after-seconds",
        type=int,
        default=int(os.getenv("MONITOR_REPEAT_AFTER_SECONDS", "3600")),
        help="repeat the same failure alert after this many seconds",
    )
    parser.add_argument(
        "--notify-recovery",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="send a recovery message after an alerted failure recovers",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    errors: List[str] = []
    ok, health = _http_get_json(args.url, args.timeout)
    if not ok:
        errors.append(f"ready check failed url={args.url} detail={json.dumps(health, ensure_ascii=False)[:800]}")

    scheduler_values = list(args.scheduler)
    env_schedulers = os.getenv("MONITOR_SCHEDULERS", "")
    if env_schedulers:
        scheduler_values.append(env_schedulers)
    if scheduler_values:
        init_db()
        for service, max_age_seconds in _parse_scheduler_specs(scheduler_values):
            error = _check_scheduler(service, max_age_seconds)
            if error:
                errors.append(error)

    state = _load_state(args.state_file)

    if not errors:
        recovery_alert = _should_send_recovery_alert(
            state=state,
            consecutive_failures=max(args.consecutive_failures, 1),
        )
        _save_state(args.state_file, state)
        print("ok")
        if recovery_alert and args.notify_recovery and args.webhook_url and not args.dry_run:
            message = "\n".join(
                [
                    "[AI4ALL][OK] health monitor recovered",
                    f"env={settings.app_env}",
                    f"checked_at={datetime.now().isoformat(timespec='seconds')}",
                ]
            )
            try:
                _send_feishu_alert(args.webhook_url, message, args.timeout)
            except Exception as err:
                print(f"feishu recovery alert send failed: {err}", file=sys.stderr)
        return 0

    should_alert = _should_send_failure_alert(
        state=state,
        errors=errors,
        consecutive_failures=max(args.consecutive_failures, 1),
        repeat_after_seconds=max(args.repeat_after_seconds, 60),
    )
    _save_state(args.state_file, state)

    message = "\n".join(
        [
            "[AI4ALL][P1] health monitor failed",
            f"env={settings.app_env}",
            f"checked_at={datetime.now().isoformat(timespec='seconds')}",
            f"consecutive_failures={state.get('failure_count')}",
            *[f"- {error}" for error in errors],
        ]
    )
    print(message)

    if should_alert and args.webhook_url and not args.dry_run:
        try:
            _send_feishu_alert(args.webhook_url, message, args.timeout)
        except Exception as err:
            print(f"feishu alert send failed: {err}", file=sys.stderr)
    elif not should_alert:
        print("alert suppressed until consecutive failure threshold is reached")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
