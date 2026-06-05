from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import (
    claim_due_proactive_account_state,
    get_proactive_account_state as db_get_proactive_account_state,
    list_due_proactive_account_states,
    upsert_proactive_account_state,
)
from app.proactive.account_checks import (
    decide_account_check_action,
    execute_account_check_decision,
    generate_content_invitation_candidate,
    generate_topic_followup_candidate,
)
from app.proactive.reactivation import plan_reactivation_candidate
from app.proactive.reactivation import dispatch_reactivation_candidate


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
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    return list_due_proactive_account_states(
        now=format_state_time(current),
        limit=limit,
    )


def claim_due_account_check(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
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


def mark_account_checked(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    next_scan_at: Optional[datetime] = None,
    interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
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


def mark_account_check_sent(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
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


def scan_due_proactive_account_checks(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    check_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    reactivation_dry_run: Optional[bool] = None,
    reactivation_dispatch_enabled: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    # Default reactivation dispatch off + dry-run; require explicit opt-in via
    # settings (or caller) before any real outbound is sent. Misconfigured
    # production deploys should fail closed, not silently push messages.
    if reactivation_dispatch_enabled is None:
        reactivation_dispatch_enabled = bool(
            getattr(settings, "reactivation_dispatch_enabled", False)
        )
    if reactivation_dry_run is None:
        reactivation_dry_run = bool(
            getattr(settings, "reactivation_dispatch_dry_run", True)
        )
    due_accounts = list_due_proactive_account_checks(now=current, limit=limit)
    results: List[Dict[str, Any]] = []
    for item in due_accounts:
        claimed = claim_due_account_check(
            account_id=item["account_id"],
            now=current,
            interval_seconds=check_interval_seconds,
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
        if not reactivation_dispatch_enabled:
            # Kill-switch off: still let planning run so candidates queue up,
            # but never invoke the sender. fall through to decide/plan path.
            reactivation_dispatch = {
                "action": "no_op",
                "account_id": claimed["account_id"],
                "reason": "reactivation_dispatch_disabled",
                "evaluated_at": format_state_time(current),
                "metadata": {},
            }
        else:
            reactivation_dispatch = dispatch_reactivation_candidate(
                account_id=claimed["account_id"],
                now=current,
                dry_run=reactivation_dry_run,
            )
        dispatch_short_circuit_actions = {
            "sent",
            "would_send",
            "delayed",
            "send_blocked",
            # not_due means a candidate is already scheduled for a later slot;
            # do NOT replan or overwrite it in this scan.
            "not_due",
        }
        dispatch_no_op_short_circuit_reasons = {
            "reactivation_daily_limit_already_sent",
            "recent_inbound_final_slot",
            "avoidance_window_final_slot",
            "dedupe_duplicate_after_retry",
            "regenerate_failed_after_new_message",
            "missing_channel_route",
        }
        if (
            reactivation_dispatch.get("action") in dispatch_short_circuit_actions
            or (
                reactivation_dispatch.get("action") == "no_op"
                and reactivation_dispatch.get("reason")
                in dispatch_no_op_short_circuit_reasons
            )
        ):
            results.append(
                {
                    "status": reactivation_dispatch.get("action"),
                    "reason": reactivation_dispatch.get("reason"),
                    "account_id": claimed["account_id"],
                    "reactivation_dispatch": reactivation_dispatch,
                    "decision": {
                        "action": "no_op",
                        "account_id": claimed["account_id"],
                        "reason": "reactivation_dispatch_handled",
                        "evaluated_at": format_state_time(current),
                    },
                    "execution": {
                        "status": "skipped",
                        "reason": "reactivation_dispatch_handled",
                    },
                    "reactivation_planning": {
                        "action": "no_op",
                        "account_id": claimed["account_id"],
                        "reason": "reactivation_dispatch_handled",
                        "evaluated_at": format_state_time(current),
                        "metadata": {},
                    },
                    "content_invitation_generation": {
                        "action": "no_op",
                        "account_id": claimed["account_id"],
                        "reason": "reactivation_dispatch_handled",
                        "evaluated_at": format_state_time(current),
                        "metadata": {},
                    },
                    "account_state": claimed,
                }
            )
            continue
        decision = decide_account_check_action(
            account_id=claimed["account_id"],
            now=current,
        )
        execution = execute_account_check_decision(
            decision=decision,
            now=current,
        )
        account_state = claimed
        if execution.get("status") == "sent":
            account_state = mark_account_check_sent(
                account_id=claimed["account_id"],
                now=current,
            )
            reactivation_planning = {
                "action": "no_op",
                "account_id": claimed["account_id"],
                "reason": "companion_followup_sent_this_check",
                "evaluated_at": format_state_time(current),
                "metadata": {},
            }
            content_invitation_generation = reactivation_planning
        else:
            reactivation_planning = plan_reactivation_candidate(
                account_id=claimed["account_id"],
                now=current,
                topic_followup_generator=generate_topic_followup_candidate,
                content_invitation_generator=generate_content_invitation_candidate,
            )
            content_invitation_generation = reactivation_planning.get(
                "content_invitation_generation",
                reactivation_planning,
            )
        results.append(
            {
                "status": execution["status"],
                "reason": execution.get("reason"),
                "account_id": claimed["account_id"],
                "decision": decision,
                "execution": execution,
                "reactivation_dispatch": reactivation_dispatch,
                "reactivation_planning": reactivation_planning,
                "content_invitation_generation": content_invitation_generation,
                "account_state": account_state,
            }
        )
    return results
