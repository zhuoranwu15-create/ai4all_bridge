"""Handlers for mission_status / record_mission_moment.

见 docs/tech_design/agent_mission_and_orchestration_design.md §6。服务端只做确定性
校验（有无使命、名额是否用尽、content 是否非空），不重新评判"这个瞬间够不够格"——够不够
格是 LLM 在决定调用 record_mission_moment 那一刻已完成的语义判断，服务端二次审查等于
把主观判断又塞回业务逻辑，违背"主流程不写业务逻辑"的架构原则（与 create_content_
invitation_candidate 不重新校验标题质量是同一立场）。
"""
from typing import Optional, TYPE_CHECKING

from app.db import count_mission_moments, record_mission_moment
from app.mission_state import resolve_account_mission, snapshot_account_mission

if TYPE_CHECKING:
    from app.turn_context import TurnContext


def handle_mission_status(args: dict, ctx: "TurnContext") -> dict:
    """查询当前账号的使命状态；无副作用。未分配使命、或 mission_id 不可解析（脏数据/
    模板下线）都统一返回 has_mission=False，而不是报错——两者对调用方而言是同一种
    "当前无有效使命"状态（见 app.mission_state.resolve_account_mission）。
    """
    snapshot = snapshot_account_mission(account_id=ctx.account_id)
    if snapshot is None:
        return {"status": "ok", "has_mission": False}

    resolved, progress, recent = snapshot
    template = resolved.template
    return {
        "status": "ok",
        "has_mission": True,
        "mission_id": template.id,
        "display_name": template.display_name,
        "statement": template.statement,
        "bar": template.bar,
        "inquiry": template.inquiry,
        "target_count": template.target_count,
        "progress": progress,
        "remaining": max(template.target_count - progress, 0),
        "recent_moments": [{"content": m["content"], "recorded_at": m["recorded_at"]} for m in recent],
    }


def handle_record_mission_moment(
    args: dict,
    ctx: "TurnContext",
    *,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    """记录一个瞬间；追加型、不可撤销。account_id 一律取 ctx，忽略 args 里的同名字段。

    未分配使命、或 mission_id 不可解析都返回同一个"尚未分配使命"错误——不应该出现
    "has_mission=True 但调用即报未知错误"的体验（脏数据/模板下线场景）。
    """
    resolved = resolve_account_mission(account_id=ctx.account_id)
    if resolved is None:
        return {"error": "尚未分配使命"}

    template = resolved.template
    progress = count_mission_moments(account_id=ctx.account_id, mission_id=template.id)
    if progress >= template.target_count:
        return {
            "status": "already_complete",
            "progress": progress,
            "target_count": template.target_count,
        }

    content = str(args.get("content") or "").strip()
    if not content:
        return {"error": "content 不能为空"}

    session_id = str(ctx.session.get("id")) if ctx.session and ctx.session.get("id") is not None else None
    moment_id = record_mission_moment(
        account_id=ctx.account_id,
        mission_id=template.id,
        content=content,
        session_id=session_id,
        message_id=ctx.message_id,
    )
    new_progress = progress + 1
    return {
        "status": "recorded",
        "moment_id": moment_id,
        "progress": new_progress,
        "target_count": template.target_count,
        "remaining": max(template.target_count - new_progress, 0),
    }
