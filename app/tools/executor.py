import logging
from importlib import import_module
from typing import Optional, TYPE_CHECKING

from app.tools.registry import (
    CALL_INVOCATION,
    CALL_WEB_SEARCH,
)

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext

logger = logging.getLogger("ai4all.tools.executor")


def execute_tool_call(
    name: str,
    args: dict,
    ctx: "TurnContext",
    *,
    tool_call_id: Optional[str] = None,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    """Dispatch a tool call to the appropriate handler. Never raises — returns error dict on failure.

    分发完全由 app.tools.registry 驱动（schema↔handler 单一事实源）。handler 按需惰性
    import，避免工具包加载期的循环导入。
    """
    policy = getattr(ctx, "tool_policy", None)
    if policy is None:
        logger.error(
            "execute_tool_call missing ToolPolicy account=%s app=%s",
            getattr(ctx, "account_id", None),
            getattr(ctx, "app_id", None),
        )
        return {"status": "failed", "error": "tool policy is required"}
    if getattr(ctx, "app_id", None) != policy.app_id:
        logger.error(
            "execute_tool_call ToolPolicy scope mismatch account=%s context_app=%s policy_app=%s",
            getattr(ctx, "account_id", None),
            getattr(ctx, "app_id", None),
            policy.app_id,
        )
        return {"status": "failed", "error": "tool policy scope mismatch"}

    known_spec = policy.catalog.get_spec(name)
    if known_spec is None:
        logger.warning("execute_tool_call unknown tool: %s", name)
        return {"error": f"未知工具: {name}"}
    spec = policy.get_spec(name)
    if spec is None:
        logger.warning(
            "execute_tool_call product disallowed tool=%s account=%s app=%s",
            name,
            getattr(ctx, "account_id", None),
            getattr(ctx, "app_id", None),
        )
        return {"status": "failed", "error": f"{name} is not allowed for this product"}

    # 运行时开关：如 web_search 被禁用，拒绝执行并返回固定失败结果。
    if spec.runtime_requires_flag and not bool(getattr(ctx, spec.runtime_requires_flag, False)):
        logger.warning(
            "%s tool called while disabled account=%s", name, getattr(ctx, "account_id", None)
        )
        return {"status": "failed", "error": f"{name} is disabled"}

    try:
        handler = getattr(import_module(spec.handler_module), spec.handler_attr)
        if spec.call_style == CALL_WEB_SEARCH:
            return handler(
                args,
                ctx,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
            )
        if spec.call_style == CALL_INVOCATION:
            return handler(args, ctx, tool_invocation_id=tool_invocation_id)
        return handler(args, ctx)
    except Exception as err:
        logger.exception("tool handler failed tool=%s error=%s", name, err)
        return {"error": str(err)}
