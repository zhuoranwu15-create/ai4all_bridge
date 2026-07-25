import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import (
    cancel_reminder,
    claim_due_reminder,
    claim_reminder_content_run,
    get_outbound_message,
    get_reminder,
    list_due_reminders,
    list_enqueued_reminder_content_runs,
    mark_reminder_content_run_enqueued,
    mark_reminder_content_run_failed,
    mark_reminder_content_run_sent,
    mark_reminder_content_run_skipped,
    mark_reminder_failed,
    mark_reminder_sent,
    reschedule_reminder_stale_touch,
    update_reminder_content_meta,
)
from app.products.zhaoxi.proactive.delivery.outbound import dispatch_proactive_text
from app.products.zhaoxi.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.products.zhaoxi.proactive.fulfillment import fulfill_dynamic_reminder
from app.reminder_utils import compute_next_due_at
from app.time_utils import beijing_naive_now

logger = logging.getLogger("ai4all.proactive.reminders")


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
    # sent=本机直发成功；pending=远程账号（如 aliyun2）已入队，待归属节点 pull 发送。
    # 两者都视为本期已交付/在途：推进 recur 周期，避免远程账号的循环提醒被误判 failed 而卡死
    # （list_due_reminders 只扫 pending，failed 后本条永不再触发）。远程若最终发送失败，仅损失
    # 这一期不重发，与动态提醒分支的取舍一致（见设计文档 §6.2-6）。
    if outbound_status in ("sent", "pending"):
        recur_rule = claimed.get("recur_rule")
        next_due_at = None
        if recur_rule:
            try:
                last_due = datetime.strptime(claimed["due_at"], "%Y-%m-%d %H:%M:%S")
                next_due_at = compute_next_due_at(recur_rule, last_due).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, IndexError):
                # recur_rule 脏数据（如 /debug 补丁写入）不阻断调度，降级为一次性终止。
                next_due_at = None
        reminder = mark_reminder_sent(
            reminder_id=claimed["id"],
            outbound_message_id=outbound_id,
            next_due_at=next_due_at,
        )
        return {
            "status": outbound_status,
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
    # 只处理固定文案提醒（fulfillment='fixed'，含所有历史提醒）；动态提醒走独立扫描，
    # 避免同一到期行被两条链路重复处理。
    due = list_due_reminders(
        now=format_scheduler_time(current),
        limit=limit,
        node_id=node_id,
        fulfillment="fixed",
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


# ---------------------------------------------------------------------------
# 动态提醒（例行简报）：到期履约合成轮次 + run 状态机 + 远程对账
# 见 docs/tech_design/dynamic_reminder_scheduled_content_design.md §6。
# ---------------------------------------------------------------------------

def _next_due_at_for(reminder: Dict[str, Any], scheduled_for: str) -> Optional[str]:
    """按 recur_rule 计算下一周期；无 recur_rule（一次性）返回 None。脏规则降级为 None。"""
    recur_rule = reminder.get("recur_rule")
    if not recur_rule:
        return None
    try:
        last_due = datetime.strptime(scheduled_for, "%Y-%m-%d %H:%M:%S")
        return compute_next_due_at(recur_rule, last_due).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None


def _advance_reminder_period(
    *, reminder: Dict[str, Any], scheduled_for: str, sent: bool, error: Optional[str] = None
) -> Dict[str, Any]:
    """把提醒推进到下一周期。

    sent=True 走 mark_reminder_sent（计入 sent_count）；否则走 reschedule（不计发送）。
    无下一周期（一次性）时：sent→标记 sent 终态；否则 cancel。
    """
    next_due_at = _next_due_at_for(reminder, scheduled_for)
    if sent:
        return mark_reminder_sent(reminder_id=reminder["id"], next_due_at=next_due_at) or {}
    if next_due_at:
        return reschedule_reminder_stale_touch(
            reminder_id=reminder["id"], next_due_at=next_due_at, error=error or "dynamic_skip"
        ) or {}
    return cancel_reminder(reminder_id=reminder["id"], error=error or "dynamic_done") or {}


def _requeue_same_period(reminder: Dict[str, Any], scheduled_for: str) -> None:
    """履约失败但仍可重试：把提醒放回 pending 且 due_at 保持本周期，下一 tick 重试同一 run。"""
    reschedule_reminder_stale_touch(
        reminder_id=reminder["id"], next_due_at=scheduled_for, error="dynamic_retry"
    )


def dispatch_dynamic_reminder(
    *,
    reminder_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    """单条动态提醒的一次到期履约。never raises（异常降级为 failed 结果）。"""
    current = now or beijing_naive_now()
    claimed = claim_due_reminder(reminder_id=reminder_id, now=format_scheduler_time(current))
    if claimed is None:
        return {"status": "skipped", "reason": "not_due_or_already_claimed", "reminder_id": reminder_id}

    scheduled_for = claimed["due_at"]
    max_attempts = max(1, int(getattr(settings, "dynamic_reminder_max_retries", 2) or 0) + 1)
    run = claim_reminder_content_run(
        reminder_id=claimed["id"],
        account_id=claimed["account_id"],
        scheduled_for=scheduled_for,
        max_attempts=max_attempts,
    )
    if run is None:
        # 本周期已发送/入队/跳过，或重试已耗尽 → 推进到下一周期，避免卡在 sending。
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False,
                                 error="run_done_or_exhausted")
        return {"status": "skipped", "reason": "run_done_or_exhausted", "reminder_id": reminder_id}

    # 可触达性检查：不可达时先不搜索（省成本），跳过本期并推进。
    if get_account_touch_state(account_id=claimed["account_id"], now=current) == STALE:
        mark_reminder_content_run_skipped(run_id=run["id"], reason="touch_stale")
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False,
                                 error="proactive_touch_stale")
        return {"status": "skipped", "reason": "proactive_touch_stale", "reminder_id": reminder_id}

    result = fulfill_dynamic_reminder(claimed, now=current)
    if not result.ok:
        mark_reminder_content_run_failed(run_id=run["id"], error=result.error or "fulfillment_failed")
        if int(run.get("attempts") or 1) < max_attempts:
            _requeue_same_period(claimed, scheduled_for)  # 保持本周期，下一 tick 重试
            return {"status": "failed", "reason": result.error, "retry": True, "reminder_id": reminder_id}
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False,
                                 error=result.error or "fulfillment_failed")
        return {"status": "failed", "reason": result.error, "retry": False, "reminder_id": reminder_id}

    try:
        outbound = dispatch_proactive_text(
            account_id=claimed["account_id"],
            channel=claimed["channel"],
            channel_account_id=claimed.get("channel_account_id"),
            to_user_id=claimed["to_user_id"],
            session_key=claimed.get("session_key"),
            source="reminder",
            text=result.text,
            idempotency_key=_reminder_idempotency_key(claimed),
            now=current,
            bypass_quiet_hours=bypass_quiet_hours,
            product_category="user_reminder",
            metadata={
                "reminder_id": claimed["id"],
                "reminder_due_at": scheduled_for,
                "fulfillment": "dynamic",
                "run_id": run["id"],
            },
        )
    except Exception as err:  # noqa: BLE001 — 出站异常降级为失败结果
        mark_reminder_content_run_failed(run_id=run["id"], error=str(err))
        if int(run.get("attempts") or 1) < max_attempts:
            _requeue_same_period(claimed, scheduled_for)
            return {"status": "failed", "reason": str(err), "retry": True, "reminder_id": reminder_id}
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False, error=str(err))
        return {"status": "failed", "reason": str(err), "retry": False, "reminder_id": reminder_id}

    outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
    outbound_status = outbound.get("status")

    if outbound_status in ("sent", "pending"):
        # sent=本机直发成功；pending=远程账号已入队（待归属节点 pull 发送）。两者都视为
        # 本期已交付/在途，推进到下一周期避免重复触发；远程最终态由对账（reconcile）落 run。
        if outbound_status == "sent":
            mark_reminder_content_run_sent(
                run_id=run["id"], outbound_message_id=outbound_id,
                generated_text=result.text, search_ok=result.search_ok, search_trace=result.search_trace,
            )
        else:
            mark_reminder_content_run_enqueued(
                run_id=run["id"], outbound_message_id=outbound_id,
                generated_text=result.text, search_ok=result.search_ok, search_trace=result.search_trace,
            )
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=True)
        update_reminder_content_meta(
            reminder_id=claimed["id"],
            patch={"last_success_run_at": format_scheduler_time(current)},
        )
        return {"status": outbound_status, "reminder_id": reminder_id, "outbound_message": outbound}

    if outbound_status == "cancelled":
        # 政策/moderation 拦截：不重试，跳过本期并推进。
        mark_reminder_content_run_skipped(run_id=run["id"], reason=outbound.get("error") or "outbound_cancelled")
        _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False,
                                 error=outbound.get("error") or "outbound_cancelled")
        return {"status": "cancelled", "reminder_id": reminder_id, "outbound_message": outbound}

    # 其它出站失败：按重试策略处理。
    mark_reminder_content_run_failed(run_id=run["id"], error=outbound.get("error") or f"outbound_status:{outbound_status}",
                                     outbound_message_id=outbound_id)
    if int(run.get("attempts") or 1) < max_attempts:
        _requeue_same_period(claimed, scheduled_for)
        return {"status": "failed", "reason": outbound.get("error"), "retry": True, "reminder_id": reminder_id}
    _advance_reminder_period(reminder=claimed, scheduled_for=scheduled_for, sent=False,
                             error=outbound.get("error") or "outbound_failed")
    return {"status": "failed", "reason": outbound.get("error"), "retry": False, "reminder_id": reminder_id}


def dispatch_due_dynamic_reminders(
    *,
    now: Optional[datetime] = None,
    limit: int = 5,
    bypass_quiet_hours: bool = False,
    node_id: Optional[str] = None,  # 兼容 scheduler 传参；动态扫描刻意忽略分片（见下）
) -> List[Dict[str, Any]]:
    """扫描到期的动态提醒并逐条履约。

    动态提醒由 aliyun1 统一调度，须覆盖归属其它节点（如 aliyun2）的账号，故**不按 node 分片**
    （node_id 传 None）；出站再由 dispatch_proactive_text 按账号归属自动 inline/enqueue。
    """
    current = now or beijing_naive_now()
    if not bool(getattr(settings, "dynamic_reminder_enabled", False)):
        return []
    due = list_due_reminders(
        now=format_scheduler_time(current),
        limit=limit,
        node_id=None,
        fulfillment="dynamic",
    )
    results: List[Dict[str, Any]] = []
    for reminder in due:
        results.append(
            dispatch_dynamic_reminder(
                reminder_id=reminder["id"],
                now=current,
                bypass_quiet_hours=bypass_quiet_hours,
            )
        )
    return results


def reconcile_enqueued_reminder_content_runs(
    *, now: Optional[datetime] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    """远程账号出站对账：把 enqueued 的 run 按关联 outbound 的最终态翻成 sent/failed。

    远程 enqueue 时 run 记为 enqueued（未终结）；归属节点 pull 发送后 outbound 落终态，
    这里据此落 run 终态，供 run 历史/观测准确。提醒周期在入队时已推进，故此处只更新 run。
    """
    _ = now  # 预留：将来可据 created_at 做 enqueued 超时兜底
    # 总开关关闭时不可能有动态履约 run，直接短路（避免无谓的 DB 访问）。
    if not bool(getattr(settings, "dynamic_reminder_enabled", False)):
        return []
    results: List[Dict[str, Any]] = []
    for run in list_enqueued_reminder_content_runs(limit=limit):
        outbound_id = run.get("outbound_message_id")
        if outbound_id is None:
            continue
        outbound = get_outbound_message(outbound_message_id=int(outbound_id))
        if outbound is None:
            continue
        status = outbound.get("status")
        if status == "sent":
            mark_reminder_content_run_sent(run_id=run["id"], outbound_message_id=int(outbound_id))
            results.append({"run_id": run["id"], "status": "sent"})
        elif status in ("failed", "cancelled"):
            mark_reminder_content_run_failed(
                run_id=run["id"], error=outbound.get("error") or f"outbound_{status}",
                outbound_message_id=int(outbound_id),
            )
            results.append({"run_id": run["id"], "status": "failed"})
        # 仍 pending/sending → 保持 enqueued，等下轮对账
    return results
