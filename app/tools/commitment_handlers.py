from datetime import timedelta
from typing import TYPE_CHECKING

from app.db import create_proactive_commitment, get_account, get_proactive_account_state
from app.proactive.contract.common import _select_route
from app.proactive.obligations.commitments import _commitment_max_days, _parse_due_at
from app.time_utils import beijing_naive_now

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext


def handle_create_commitment(args: dict, ctx: "TurnContext") -> dict:
    text = str(args.get("text") or "").strip()[:120]
    due_at_raw = str(args.get("due_at") or "").strip()
    reason = str(args.get("reason") or "").strip() or "tool_use"
    if not text:
        return {"error": "跟进内容不能为空"}

    account = get_account(account_id=ctx.account_id)
    if account is None or account.get("status") != "active":
        return {"error": "账号不可用，无法记录"}
    state = get_proactive_account_state(account_id=ctx.account_id)
    if state is None or not state.get("enabled"):
        return {"status": "skipped", "reason": "proactive_disabled"}
    if _select_route(ctx.account_id) is None:
        return {"status": "skipped", "reason": "missing_channel_route"}

    due_dt = _parse_due_at(due_at_raw)
    now = beijing_naive_now()
    if due_dt is None or due_dt <= now:
        return {"error": "due_at 必须是未来时间，格式 YYYY-MM-DD HH:MM:SS"}
    max_days = _commitment_max_days()
    if due_dt > now + timedelta(days=max(max_days, 1)):
        return {"error": f"due_at 不能超过 {max_days} 天后"}

    item = create_proactive_commitment(
        account_id=ctx.account_id,
        session_id=int(ctx.session["id"]),
        source_message_id=ctx.message_id,
        text=text,
        due_at=due_dt.strftime("%Y-%m-%d %H:%M:%S"),
        reason=reason,
        dedupe_key=f"commitment:tool:{ctx.account_id}:{ctx.message_id}",
        metadata={"source": "tool_use", "source_message_id": ctx.message_id},
    )
    return {"status": "created", "commitment_id": item["id"], "due_at": item["due_at"]}
