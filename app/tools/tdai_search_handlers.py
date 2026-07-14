"""TDAI 主动检索工具 handler。

两个工具让模型在 tool loop 中途按需检索长期记忆（L1）和原始对话（L0），补被动 recall
的盲区。两者共用 ctx 上的 per-turn 计数器，合计次数受 tdai_search_max_calls_per_turn 限制
（TDAI 后端未实现该硬上限，由本侧强制）。

隔离契约：account_id 一律取自 ctx，忽略 args 里的同名字段；session_key 在 tdai_client 内由
tdai_session_key(account_id) 强制注入。失败/空结果降级为 benign 文本，不打断本轮生成。
"""
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.config import settings
from app.tdai_client import search_conversations, search_memories

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.tdai_search")

_EMPTY_RESULT = "（未检索到相关内容）"


def _limit_arg(value: Any) -> int:
    """把 limit 夹到 1-20，非法/缺省取 5（与 TDAI 后端默认一致）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return min(max(parsed, 1), 20)


def _check_and_bump_quota(ctx: "TurnContext") -> Optional[Dict[str, Any]]:
    """per-turn 限流：超限返回失败结果 dict，否则自增计数并返回 None。

    两个 handler 共用 ctx.tdai_search_calls，合计生效；单轮内多个 tool_call 在 llm.py 顺序
    执行，无并发竞态。
    """
    cap = int(getattr(settings, "tdai_search_max_calls_per_turn", 3) or 3)
    if ctx.tdai_search_calls >= cap:
        logger.info(
            "tdai search per-turn limit reached account=%s cap=%s",
            getattr(ctx, "account_id", None), cap,
        )
        return {"status": "failed", "error": f"本轮记忆检索次数已达上限（{cap} 次）"}
    ctx.tdai_search_calls += 1
    return None


def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
    """把 client 返回（可能为空）归一为 benign 成功结果。"""
    text = (result.get("results") or "").strip() if result else ""
    out: Dict[str, Any] = {
        "status": "succeeded",
        "results": text or _EMPTY_RESULT,
        "total": int(result.get("total", 0)) if result else 0,
    }
    if result and result.get("strategy"):
        out["strategy"] = result["strategy"]
    return out


def handle_tdai_memory_search(args: dict, ctx: "TurnContext") -> dict:
    quota = _check_and_bump_quota(ctx)
    if quota is not None:
        return quota
    query = str(args.get("query") or "").strip()
    if not query:
        return {"status": "failed", "error": "query is required"}
    type_filter = args.get("type") if isinstance(args.get("type"), str) else None
    scene_filter = args.get("scene") if isinstance(args.get("scene"), str) else None
    result = search_memories(
        account_id=ctx.account_id,
        query=query,
        limit=_limit_arg(args.get("limit")),
        type=type_filter,
        scene=scene_filter,
    )
    return _ok(result)


def handle_tdai_conversation_search(args: dict, ctx: "TurnContext") -> dict:
    quota = _check_and_bump_quota(ctx)
    if quota is not None:
        return quota
    query = str(args.get("query") or "").strip()
    if not query:
        return {"status": "failed", "error": "query is required"}
    result = search_conversations(
        account_id=ctx.account_id,
        query=query,
        limit=_limit_arg(args.get("limit")),
    )
    return _ok(result)
