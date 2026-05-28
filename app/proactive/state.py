from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.db import (
    claim_due_proactive_account_state,
    get_proactive_account_state as db_get_proactive_account_state,
    list_due_proactive_account_states,
    upsert_proactive_account_state,
)
from app.proactive.heartbeat import (
    decide_heartbeat_action,
    execute_heartbeat_decision,
)


DEFAULT_SCAN_INTERVAL_SECONDS = 60 * 60


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


def list_due_proactive_accounts(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    return list_due_proactive_account_states(
        now=format_state_time(current),
        limit=limit,
    )


def claim_due_account_scan(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
) -> Optional[Dict[str, Any]]:
    current = now or datetime.now()
    next_scan = next_scan_at or (
        current + timedelta(seconds=max(int(interval_seconds), 1))
    )
    return claim_due_proactive_account_state(
        account_id=account_id,
        now=format_state_time(current),
        next_scan_at=format_state_time(next_scan),
    )


def mark_account_scanned(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    current = now or datetime.now()
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
    current = now or datetime.now()
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


def mark_account_heartbeat_sent(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    state = get_account_state(account_id=account_id)
    metadata = dict((state or {}).get("metadata") or {})
    sent_candidate = metadata.pop("heartbeat_candidate", None)
    if sent_candidate is not None:
        metadata["heartbeat_last_sent_candidate"] = sent_candidate
    metadata["heartbeat_candidate_sent_at"] = format_state_time(current)
    return upsert_proactive_account_state(
        account_id=account_id,
        last_proactive_sent_at=format_state_time(current),
        metadata=metadata,
    )


def scan_due_proactive_accounts(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    due_accounts = list_due_proactive_accounts(now=current, limit=limit)
    results: List[Dict[str, Any]] = []
    for item in due_accounts:
        claimed = claim_due_account_scan(
            account_id=item["account_id"],
            now=current,
            interval_seconds=scan_interval_seconds,
        )
        if claimed is None:
            results.append(
                {
                    "status": "skipped",
                    "reason": "not_due_or_already_claimed",
                    "account_id": item["account_id"],
                }
            )
            continue
        decision = decide_heartbeat_action(
            account_id=claimed["account_id"],
            now=current,
        )
        execution = execute_heartbeat_decision(
            decision=decision,
            now=current,
        )
        account_state = claimed
        if execution.get("status") == "sent":
            account_state = mark_account_heartbeat_sent(
                account_id=claimed["account_id"],
                now=current,
            )
        results.append(
            {
                "status": execution["status"],
                "reason": execution.get("reason"),
                "account_id": claimed["account_id"],
                "decision": decision,
                "execution": execution,
                "account_state": account_state,
            }
        )
    return results
