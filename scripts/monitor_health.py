import argparse
import json
import os
import shutil
import subprocess
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
from app.db import (  # noqa: E402
    get_account_water_level,
    get_scheduler_heartbeat,
    init_db,
)
from app.time_utils import beijing_naive_now  # noqa: E402

DEFAULT_STATE_FILE = "/tmp/ai4all_monitor_health_state.json"


def _ensure_schema_best_effort() -> None:
    """建表兜底：监控只读现有表，被 PG 迁移闸拦下时不该把整轮健康检查带崩。

    闸门见 ``app.db._core._guard_unattended_pg_migrations``——`git pull` 之后、服务重启之前
    会短暂存在「有待执行迁移」的窗口，此时监控（未 opt-in）会被拒；表其实早已存在，继续跑
    即可，真缺表时后续查询自会报错。
    """
    try:
        init_db()
    except RuntimeError as err:
        print(f"init_db skipped: {err}", file=sys.stderr)


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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _check_scheduler(service: str, max_age_seconds: int) -> Optional[str]:
    heartbeat = get_scheduler_heartbeat(service)
    if heartbeat is None:
        return f"{service}: heartbeat missing"
    last_seen_at = _parse_timestamp(heartbeat.get("last_seen_at"))
    if last_seen_at is None:
        return f"{service}: invalid last_seen_at={heartbeat.get('last_seen_at')}"
    # last_seen_at 为北京墙钟 naive(见 db.ops._runtime_timestamp),陈旧度比较须同口径,
    # 否则非北京宿主机会误判心跳新鲜/陈旧。
    age_seconds = int((beijing_naive_now() - last_seen_at).total_seconds())
    if age_seconds > max_age_seconds:
        return f"{service}: heartbeat stale age={age_seconds}s max={max_age_seconds}s"
    if heartbeat.get("status") in {"error", "failed"}:
        return f"{service}: last status={heartbeat.get('status')} error={heartbeat.get('last_error') or ''}".strip()
    return None


def _parse_backup_stamp(name: str) -> Optional[datetime]:
    """从备份目录名 ai4all_YYYYMMDD_HHMMSS 解析时间；不匹配返回 None。"""
    prefix = "ai4all_"
    if not name.startswith(prefix):
        return None
    try:
        return datetime.strptime(name[len(prefix):], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _check_backup_staleness(backups_dir: Path, max_age_seconds: int) -> Optional[str]:
    """最新本地备份超过 max_age_seconds（或一份都没有）时返回错误串。

    以备份目录本身的时间戳为准（持久、重启不丢），不依赖 /tmp 状态文件。
    max_age_seconds<=0 表示关闭该检查。
    """
    if max_age_seconds <= 0:
        return None
    if not backups_dir.is_dir():
        return f"backup: directory missing {backups_dir}"
    stamps = [
        stamp
        for stamp in (_parse_backup_stamp(p.name) for p in backups_dir.iterdir() if p.is_dir())
        if stamp is not None
    ]
    if not stamps:
        return f"backup: no backups found in {backups_dir}"
    newest = max(stamps)
    age_seconds = int((datetime.now() - newest).total_seconds())
    if age_seconds > max_age_seconds:
        return (
            f"backup: latest stale age={age_seconds}s max={max_age_seconds}s "
            f"newest={newest.isoformat(timespec='seconds')}"
        )
    return None


def _check_disk_usage(
    path: str,
    *,
    max_used_percent: float,
    min_free_bytes: int,
) -> Optional[str]:
    """``path`` 所在文件系统使用率超阈值或剩余空间过低时返回错误串。

    日志留存延长到 3 年后磁盘是新的容量风险点，这里盯住日志所在卷（默认 /var/log）。
    用 ``shutil.disk_usage`` 取整个挂载点的容量，只需对 path 有 traverse 权限，
    不需要读取目录内容（监控以 ai4all 身份运行，读不了 0750 的 /var/log/nginx）。
    max_used_percent<=0 关闭使用率检查；min_free_bytes<=0 关闭剩余空间检查；两者都关则跳过。
    """
    if max_used_percent <= 0 and min_free_bytes <= 0:
        return None
    try:
        usage = shutil.disk_usage(path)
    except Exception as err:  # noqa: BLE001 — 路径不存在/无权限都应显式告警，不静默
        return f"disk: usage unavailable for {path}: {err}"

    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
    problems: List[str] = []
    if max_used_percent > 0 and used_percent >= max_used_percent:
        problems.append(f"used={used_percent:.1f}% max={max_used_percent:.0f}%")
    if min_free_bytes > 0 and usage.free < min_free_bytes:
        problems.append(f"free={usage.free}B min={min_free_bytes}B")
    if not problems:
        return None
    return f"disk: {path} low ({', '.join(problems)} total={usage.total}B)"


def _record_water_level(record_file: str) -> Optional[str]:
    """采集账号挂载水位快照并以 JSONL 追加落盘，用于建立规模化前基线。

    这是测量旁路：不参与健康告警，失败只返回错误串供调用方打印，绝不阻断健康检查。
    成功时打印一行人类可读摘要并返回 None。
    """
    try:
        snapshot = get_account_water_level()
    except Exception as err:  # noqa: BLE001 — 测量旁路，任何异常都不应中断监控
        return f"water-level: snapshot failed: {err}"

    record = {"ts": datetime.now().isoformat(timespec="seconds"), **snapshot}
    try:
        path = Path(record_file)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as err:  # noqa: BLE001
        return f"water-level: write failed: {err}"

    active = snapshot.get("active_accounts") or {}
    active_summary = " ".join(f"active{w}m={n}" for w, n in active.items())
    print(
        f"water-level recorded total={snapshot.get('total_bound_accounts')} {active_summary}".strip()
    )
    return None


def _run_command(command: List[str], timeout: float) -> Tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout, 1.0),
        )
    except subprocess.TimeoutExpired:
        return False, f"command timed out: {' '.join(command)}"
    except Exception as err:
        return False, str(err)
    output = "\n".join(
        item.strip() for item in [completed.stdout, completed.stderr] if item and item.strip()
    )
    if completed.returncode != 0:
        return False, output or f"command failed rc={completed.returncode}"
    return True, output


def _check_openclaw(channel: str, timeout: float) -> Optional[str]:
    # 与 openclaw_gateway 一致用 settings.openclaw_cli_path，而非裸 "openclaw"：
    # systemd 服务 PATH 不含 ~/.openclaw/bin（那只是交互式 shell 的 alias），
    # 裸命令会 Errno 2 导致健康探测长期假失败（见 OPENCLAW_CLI_PATH）。
    cli = settings.openclaw_cli_path
    ok, output = _run_command([cli, "channels", "status", "--probe"], timeout)
    if not ok:
        return f"openclaw status failed: {output[:800]}"

    cleaned_channel = channel.strip()
    if not cleaned_channel:
        return None

    ok, output = _run_command([cli, "channels", "list"], timeout)
    if not ok:
        return f"openclaw channels list failed: {output[:800]}"
    if cleaned_channel not in output:
        return f"openclaw channel missing: {cleaned_channel}"

    channel_line = ""
    for line in output.splitlines():
        if cleaned_channel in line:
            channel_line = line.strip()
            break
    normalized_line = channel_line.lower()
    if channel_line and (
        "disabled" in normalized_line
        or "not enabled" in normalized_line
        or "enabled" not in normalized_line
    ):
        return f"openclaw channel not enabled: {channel_line[:500]}"
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
        "--check-openclaw",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MONITOR_CHECK_OPENCLAW", False),
        help="check OpenClaw gateway/channel status via openclaw CLI",
    )
    parser.add_argument(
        "--openclaw-channel",
        default=os.getenv("MONITOR_OPENCLAW_CHANNEL", "openclaw-weixin"),
        help="configured OpenClaw channel expected in `openclaw channels list`; empty disables list check",
    )
    parser.add_argument(
        "--check-backup",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MONITOR_CHECK_BACKUP", True),
        help="alert when the latest local backup is too old",
    )
    parser.add_argument(
        "--backup-dir",
        default=os.getenv("MONITOR_BACKUP_DIR", getattr(settings, "backup_dir", "data/backups")),
        help="backup directory to inspect for staleness",
    )
    parser.add_argument(
        "--backup-max-age-seconds",
        type=int,
        default=int(os.getenv("MONITOR_BACKUP_MAX_AGE_SECONDS", "93600")),
        help="alert when newest backup is older than this (default 26h); 0 disables",
    )
    parser.add_argument(
        "--check-disk",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MONITOR_CHECK_DISK", True),
        help="alert when the log filesystem is running low on space",
    )
    parser.add_argument(
        "--disk-path",
        default=os.getenv("MONITOR_DISK_PATH", "/var/log"),
        help="filesystem path to check for free space (default /var/log, where nginx logs live)",
    )
    parser.add_argument(
        "--disk-max-used-percent",
        type=float,
        default=float(os.getenv("MONITOR_DISK_MAX_USED_PERCENT", "85")),
        help="alert when the filesystem usage reaches this percent (default 85); 0 disables",
    )
    parser.add_argument(
        "--disk-min-free-bytes",
        type=int,
        default=int(os.getenv("MONITOR_DISK_MIN_FREE_BYTES", "0")),
        help="alert when free space drops below this many bytes (default 0=disabled)",
    )
    parser.add_argument(
        "--record-water-level",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("MONITOR_RECORD_WATER_LEVEL", False),
        help="append an account water-level snapshot (bound/active counts) to a JSONL file for scaling baseline",
    )
    parser.add_argument(
        "--water-level-file",
        default=os.getenv("MONITOR_WATER_LEVEL_FILE", "data/water_level.jsonl"),
        help="JSONL file to append water-level snapshots to (relative paths resolve against repo root)",
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
        _ensure_schema_best_effort()
        for service, max_age_seconds in _parse_scheduler_specs(scheduler_values):
            error = _check_scheduler(service, max_age_seconds)
            if error:
                errors.append(error)

    if args.check_openclaw:
        error = _check_openclaw(args.openclaw_channel, args.timeout)
        if error:
            errors.append(error)

    if args.check_backup:
        backups_dir = Path(args.backup_dir)
        if not backups_dir.is_absolute():
            backups_dir = ROOT / backups_dir
        error = _check_backup_staleness(backups_dir, args.backup_max_age_seconds)
        if error:
            errors.append(error)

    if args.check_disk:
        error = _check_disk_usage(
            args.disk_path,
            max_used_percent=args.disk_max_used_percent,
            min_free_bytes=args.disk_min_free_bytes,
        )
        if error:
            errors.append(error)

    # 水位采集是测量旁路：每次运行都采，独立于健康告警（不进 errors），失败只打到 stderr。
    if args.record_water_level:
        _ensure_schema_best_effort()
        wl_error = _record_water_level(args.water_level_file)
        if wl_error:
            print(wl_error, file=sys.stderr)

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
