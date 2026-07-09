from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db import (
    cancel_reminder,
    claim_due_reminder,
    list_due_reminders,
    mark_reminder_failed,
    mark_reminder_sent,
    reschedule_reminder_stale_touch,
)
from app.proactive.delivery.outbound import dispatch_proactive_text
from app.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.reminder_utils import compute_next_due_at
from app.time_utils import beijing_naive_now


def format_scheduler_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _reminder_idempotency_key(reminder: Dict[str, Any]) -> str:
    """为每个到期周期生成独立的幂等键。

    周期提醒(recur_rule)的行 id 永不变,只把 due_at 往后推。若键只用 id,
    第二次触发时 create_outbound_message 的 INSERT OR IGNORE 会命中上一周期
    已 sent 的出站行,被去重短路而不再发送,导致之后每个周期都静默丢失。
    把当次 due_at 编进键,保证每个周期是独立的幂等单元。
    """
    due_at = reminder.get("due_at")
    base = f"reminder-{reminder['id']}"
    if not due_at:
        return base
    # 去掉日期里的连字符/冒号/空格,得到适合做幂等键并透传给网关的紧凑时间戳。
    compact = due_at.replace("-", "").replace(":", "").replace(" ", "")
    return f"{base}-{compact}"


def dispatch_reminder(
    *,
    reminder_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
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

    if get_account_touch_state(account_id=claimed["account_id"], now=current) == STALE:
        recur_rule = claimed.get("recur_rule")
        next_due_at = None
        if recur_rule:
            try:
                last_due = datetime.strptime(claimed["due_at"], "%Y-%m-%d %H:%M:%S")
                next_due_at = compute_next_due_at(recur_rule, last_due).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                # recur_rule 格式非法（如经 /debug 补丁写入）：不让一条脏数据阻断整批调度，
                # 降级为一次性终止而不是抛出异常。
                next_due_at = None
        if next_due_at:
            reminder = reschedule_reminder_stale_touch(
                reminder_id=claimed["id"],
                next_due_at=next_due_at,
                error="proactive_touch_stale",
            )
        else:
            reminder = cancel_reminder(
                reminder_id=claimed["id"],
                error="proactive_touch_stale",
            )
        return {
            "status": "skipped",
            "reason": "proactive_touch_stale",
            "reminder": reminder,
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
            idempotency_key=_reminder_idempotency_key(claimed),
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
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    current = now or beijing_naive_now()
    due = list_due_reminders(
        now=format_scheduler_time(current),
        limit=limit,
        node_id=node_id,
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
