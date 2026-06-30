from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.time_utils import beijing_naive_now
from app.db import (
    claim_due_proactive_account_state,
    get_proactive_account_state as db_get_proactive_account_state,
    list_due_proactive_account_states,
    upsert_proactive_account_state,
)


DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS = 60 * 60


def format_state_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def get_account_state(*, account_id: str) -> Optional[Dict[str, Any]]:
    return db_get_proactive_account_state(account_id=account_id)


def ensure_account_state(
    *,
    account_id: str,
    enabled: bool = True,
    next_scan_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = get_account_state(account_id=account_id)
    if current is not None:
        return current
    return upsert_proactive_account_state(
        account_id=account_id,
        enabled=enabled,
        next_scan_at=format_state_time(next_scan_at) if next_scan_at else None,
        metadata=metadata or {},
    )


def set_account_enabled(
    *,
    account_id: str,
    enabled: bool,
    next_scan_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "account_id": account_id,
        "enabled": enabled,
    }
    if next_scan_at is not None:
        kwargs["next_scan_at"] = format_state_time(next_scan_at)
    return upsert_proactive_account_state(**kwargs)


def list_due_proactive_account_checks(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    current = now or beijing_naive_now()
    return list_due_proactive_account_states(
        now=format_state_time(current),
        limit=limit,
        node_id=node_id,
    )


def claim_due_account_check(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
) -> Optional[Dict[str, Any]]:
    current = now or beijing_naive_now()
    next_scan = next_scan_at or (
        current + timedelta(seconds=max(int(interval_seconds), 1))
    )
    return claim_due_proactive_account_state(
        account_id=account_id,
        now=format_state_time(current),
        next_scan_at=format_state_time(next_scan),
    )


def mark_account_checked(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    next_scan = next_scan_at or (
        current + timedelta(seconds=max(int(interval_seconds), 1))
    )
    return upsert_proactive_account_state(
        account_id=account_id,
        last_scan_at=format_state_time(current),
        next_scan_at=format_state_time(next_scan),
    )


def mark_account_proactive_sent(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    cooldown_until: Optional[datetime] = None,
    cooldown_seconds: Optional[int] = None,
    next_scan_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    if cooldown_until is None and cooldown_seconds is not None:
        cooldown_until = current + timedelta(seconds=max(int(cooldown_seconds), 1))

    kwargs: Dict[str, Any] = {
        "account_id": account_id,
        "last_proactive_sent_at": format_state_time(current),
    }
    if cooldown_until is not None:
        kwargs["cooldown_until"] = format_state_time(cooldown_until)
    if next_scan_at is not None:
        kwargs["next_scan_at"] = format_state_time(next_scan_at)
    return upsert_proactive_account_state(**kwargs)


def mark_account_check_sent(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    state = get_account_state(account_id=account_id)
    metadata = (state or {}).get("metadata") or {}
    # Rename {account_check_candidate, heartbeat_candidate} → last_sent.
    # Use metadata_patch to scope the write to only these keys so concurrent
    # writers cannot lose sibling metadata (e.g., reactivation_candidate).
    sent_candidate = metadata.get("account_check_candidate")
    if sent_candidate is None:
        sent_candidate = metadata.get("heartbeat_candidate")
    patch: Dict[str, Any] = {
        "account_check_candidate": None,
        "heartbeat_candidate": None,
        "account_check_candidate_sent_at": format_state_time(current),
    }
    if sent_candidate is not None:
        patch["account_check_last_sent_candidate"] = sent_candidate
    return upsert_proactive_account_state(
        account_id=account_id,
        last_proactive_sent_at=format_state_time(current),
        metadata_patch=patch,
    )


def __getattr__(name):  # PEP 562 惰性再导出，避免 state<->planning 循环 import
    """`scan_due_proactive_account_checks` 已搬到 planning.py；保留旧导入路径。"""
    if name == "scan_due_proactive_account_checks":
        from app.proactive.planning import scan_due_proactive_account_checks as _f

        return _f
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
