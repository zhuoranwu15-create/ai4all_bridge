import logging
from typing import TYPE_CHECKING

from app.config import settings
from app.db import (
    cancel_reminder,
    count_active_dynamic_reminders_for_account,
    create_reminder,
    get_reminder,
    list_reminders_for_account,
    update_reminder,
)
from app.products.zhaoxi.proactive.fulfillment import is_dynamic_reminder_allowed
from app.products.zhaoxi.proactive.obligations.reminder_schedule import (
    compute_first_due_at,
    validate_due_at,
    validate_recur_rule,
)
from app.time_utils import beijing_naive_now

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext

logger = logging.getLogger("ai4all.tools.reminder_handlers")


def handle_create_reminder(args: dict, ctx: "TurnContext") -> dict:
    # Accept DSML alias names alongside the canonical parameter names.
    text = str(args.get("text") or args.get("title") or args.get("content") or "").strip()
    due_at_raw = str(args.get("due_at") or args.get("time") or args.get("datetime") or "").strip()
    recur_rule_raw = args.get("recur_rule") or args.get("repeat") or args.get("recurrence")
    fulfillment = str(args.get("fulfillment") or "fixed").strip().lower() or "fixed"
    if fulfillment not in ("fixed", "dynamic"):
        return {"error": "fulfillment 只能是 fixed 或 dynamic"}

    if not text:
        return {"error": "提醒内容不能为空"}
    try:
        due_dt = validate_due_at(due_at_raw)
    except ValueError as e:
        return {"error": f"时间格式无效：{e}"}

    recur_rule = None
    if recur_rule_raw:
        # Normalize bare "weekly"/"monthly" (no day/date suffix) using the due_at anchor.
        normalized_raw = str(recur_rule_raw).strip().lower()
        if normalized_raw == "weekly":
            normalized_raw = f"weekly:{due_dt.weekday()}"  # 0=Mon, 5=Sat, 6=Sun
        elif normalized_raw == "monthly":
            normalized_raw = f"monthly:{due_dt.day}"
        try:
            recur_rule = validate_recur_rule(normalized_raw)
        except ValueError as e:
            return {"error": f"周期规则无效：{e}"}

    to_user_id = ctx.binding.get("chat_id") or getattr(ctx.identity, "chat_id", None)
    if not to_user_id:
        return {"error": "缺少发送目标，无法创建提醒"}

    content_meta = None
    due_at_str = due_dt.strftime("%Y-%m-%d %H:%M:%S")
    if fulfillment == "dynamic":
        # 灰度准入 + 每账号活跃上限。
        if not is_dynamic_reminder_allowed(ctx.account_id):
            return {"error": "例行简报（定时内容推送）功能当前未对你开放"}
        max_active = int(getattr(settings, "dynamic_reminder_max_active_per_account", 5) or 5)
        if count_active_dynamic_reminders_for_account(account_id=ctx.account_id) >= max_active:
            return {"error": f"例行简报数量已达上限（{max_active} 个），请先取消一个再新建"}
        # 首次触发时间由后端按 recur_rule + 时刻重算，绝不采用模型提供的日期。
        if recur_rule and (recur_rule == "daily" or recur_rule.startswith("weekly:")):
            due_at_str = compute_first_due_at(
                recur_rule, due_dt.time(), beijing_naive_now()
            ).strftime("%Y-%m-%d %H:%M:%S")
        max_items_raw = args.get("max_items")
        try:
            max_items = max(3, min(int(max_items_raw), 5)) if max_items_raw is not None else 5
        except (TypeError, ValueError):
            max_items = 5
        content_meta = {
            "topic": str(args.get("topic") or text).strip(),
            "max_items": max_items,
        }
        if args.get("instructions"):
            content_meta["instructions"] = str(args["instructions"]).strip()

    reminder = create_reminder(
        account_id=ctx.account_id,
        channel=ctx.identity.channel,
        channel_account_id=ctx.identity.channel_account_id,
        to_user_id=to_user_id,
        session_key=ctx.identity.session_key,
        text=text,
        due_at=due_at_str,
        recur_rule=recur_rule,
        metadata={"source": "tool_use", "source_message_id": ctx.message_id},
        fulfillment=fulfillment,
        content_meta=content_meta,
    )
    return {
        "status": "created",
        "reminder_id": reminder["id"],
        "due_at": reminder["due_at"],
        "recur_rule": recur_rule,
        "fulfillment": fulfillment,
    }


def handle_list_reminders(args: dict, ctx: "TurnContext") -> dict:
    reminders = list_reminders_for_account(
        account_id=ctx.account_id,
        status="pending",
        limit=20,
    )
    return {
        "reminders": [
            {
                "id": r["id"],
                "text": r["text"],
                "due_at": r["due_at"],
                "recur_rule": r.get("recur_rule"),
                "fulfillment": r.get("fulfillment", "fixed"),
            }
            for r in reminders
        ]
    }


def handle_cancel_reminder(args: dict, ctx: "TurnContext") -> dict:
    reminder_id = str(args.get("reminder_id", "")).strip()
    if not reminder_id:
        return {"error": "reminder_id 不能为空"}
    reminder = get_reminder(reminder_id=reminder_id)
    if not reminder or reminder["account_id"] != ctx.account_id:
        return {"error": "提醒不存在或无权操作"}
    if reminder["status"] != "pending":
        return {"error": f"该提醒状态为 {reminder['status']}，无法取消"}
    cancel_reminder(reminder_id=reminder_id)
    return {"status": "cancelled", "reminder_id": reminder_id, "text": reminder["text"]}


def handle_update_reminder(args: dict, ctx: "TurnContext") -> dict:
    reminder_id = str(args.get("reminder_id", "")).strip()
    if not reminder_id:
        return {"error": "reminder_id 不能为空"}
    reminder = get_reminder(reminder_id=reminder_id)
    if not reminder or reminder["account_id"] != ctx.account_id:
        return {"error": "提醒不存在或无权操作"}
    if reminder["status"] != "pending":
        return {"error": f"该提醒状态为 {reminder['status']}，无法修改"}

    update_kwargs: dict = {}
    if "text" in args and args["text"] is not None:
        update_kwargs["text"] = str(args["text"]).strip()
    if "due_at" in args and args["due_at"] is not None:
        try:
            dt = validate_due_at(str(args["due_at"]))
            update_kwargs["due_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as e:
            return {"error": f"时间格式无效：{e}"}
    if "recur_rule" in args:
        if args["recur_rule"] is None:
            update_kwargs["clear_recur_rule"] = True
        else:
            try:
                update_kwargs["recur_rule"] = validate_recur_rule(str(args["recur_rule"]))
            except ValueError as e:
                return {"error": f"周期规则无效：{e}"}

    if not update_kwargs:
        return {"error": "没有提供要修改的字段"}

    updated = update_reminder(reminder_id=reminder_id, **update_kwargs)
    if not updated:
        return {"error": "更新失败"}
    return {
        "status": "updated",
        "reminder": {
            "id": updated["id"],
            "text": updated["text"],
            "due_at": updated["due_at"],
            "recur_rule": updated.get("recur_rule"),
        },
    }
