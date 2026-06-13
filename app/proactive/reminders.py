from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db import (
    cancel_reminder,
    claim_due_reminder,
    list_due_reminders,
    mark_reminder_failed,
    mark_reminder_sent,
)
from app.proactive.messaging import dispatch_proactive_text
from app.reminder_utils import compute_next_due_at


def format_scheduler_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def dispatch_reminder(
    *,
    reminder_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    current = now or datetime.now()
    claimed = claim_due_reminder(
        reminder_id=reminder_id,
        now=format_scheduler_time(current),
    )
    if claimed is None:
        return {
            "status": "skipped",
            "reason": "not_due_or_already_claimed",
            "reminder_id": reminder_id,
        }

    try:
        outbound = dispatch_proactive_text(
            account_id=claimed["account_id"],
            channel=claimed["channel"],
            channel_account_id=claimed.get("channel_account_id"),
            to_user_id=claimed["to_user_id"],
            session_key=claimed.get("session_key"),
            source="reminder",
            text=claimed["text"],
            idempotency_key=f"reminder-{claimed['id']}",
            now=current,
            bypass_quiet_hours=bypass_quiet_hours,
            product_category="user_reminder",
            metadata={
                "reminder_id": claimed["id"],
                "reminder_due_at": claimed["due_at"],
            },
        )
    except Exception as err:
        reminder = mark_reminder_failed(
            reminder_id=claimed["id"],
            error=str(err),
        )
        return {
            "status": "failed",
            "reminder": reminder,
            "error": str(err),
        }

    outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
    outbound_status = outbound.get("status")
    if outbound_status == "sent":
        recur_rule = claimed.get("recur_rule")
        next_due_at = None
        if recur_rule:
            last_due = datetime.strptime(claimed["due_at"], "%Y-%m-%d %H:%M:%S")
            next_dt = compute_next_due_at(recur_rule, last_due)
            next_due_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
        reminder = mark_reminder_sent(
            reminder_id=claimed["id"],
            outbound_message_id=outbound_id,
            next_due_at=next_due_at,
        )
        return {
            "status": "sent",
            "reminder": reminder,
            "outbound_message": outbound,
        }
    if outbound_status == "cancelled":
        reminder = cancel_reminder(
            reminder_id=claimed["id"],
            outbound_message_id=outbound_id,
            error=outbound.get("error") or "outbound_cancelled",
        )
        return {
            "status": "cancelled",
            "reminder": reminder,
            "outbound_message": outbound,
        }

    reminder = mark_reminder_failed(
        reminder_id=claimed["id"],
        outbound_message_id=outbound_id,
        error=outbound.get("error") or f"outbound_status:{outbound_status}",
    )
    return {
        "status": "failed",
        "reminder": reminder,
        "outbound_message": outbound,
    }


def dispatch_due_reminders(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    bypass_quiet_hours: bool = False,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    due = list_due_reminders(
        now=format_scheduler_time(current),
        limit=limit,
    )
    results: List[Dict[str, Any]] = []
    for reminder in due:
        results.append(
            dispatch_reminder(
                reminder_id=reminder["id"],
                now=current,
                bypass_quiet_hours=bypass_quiet_hours,
            )
        )
    return results
