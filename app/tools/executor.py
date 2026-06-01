import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.executor")


def execute_tool_call(
    name: str,
    args: dict,
    ctx: "TurnContext",
    *,
    tool_call_id: Optional[str] = None,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    """Dispatch a tool call to the appropriate handler. Never raises — returns error dict on failure."""
    from app.tools.reminder_handlers import (
        handle_cancel_reminder,
        handle_create_reminder,
        handle_list_reminders,
        handle_update_reminder,
    )
    from app.tools.content_invitation_handlers import (
        handle_create_content_invitation_candidate,
        handle_record_content_invitation_feedback,
        handle_send_content_invitation_titles,
        handle_skip_content_invitation,
    )
    handlers = {
        "create_reminder": handle_create_reminder,
        "list_reminders": handle_list_reminders,
        "cancel_reminder": handle_cancel_reminder,
        "update_reminder": handle_update_reminder,
        "create_content_invitation_candidate": handle_create_content_invitation_candidate,
        "skip_content_invitation": handle_skip_content_invitation,
    }
    content_invitation_handlers = {
        "send_content_invitation_titles": handle_send_content_invitation_titles,
        "record_content_invitation_feedback": handle_record_content_invitation_feedback,
    }
    if name == "web_search":
        if not bool(getattr(ctx, "web_search_enabled", False)):
            logger.warning("web_search tool called while disabled account=%s", getattr(ctx, "account_id", None))
            return {"status": "failed", "error": "web_search is disabled"}
        from app.tools.web_search_handlers import handle_web_search

        try:
            return handle_web_search(
                args,
                ctx,
                tool_call_id=tool_call_id,
                tool_invocation_id=tool_invocation_id,
            )
        except Exception as err:
            logger.exception("tool handler failed tool=%s error=%s", name, err)
            return {"error": str(err)}

    handler = handlers.get(name)
    if name in content_invitation_handlers:
        try:
            return content_invitation_handlers[name](
                args,
                ctx,
                tool_invocation_id=tool_invocation_id,
            )
        except Exception as err:
            logger.exception("tool handler failed tool=%s error=%s", name, err)
            return {"error": str(err)}
    if handler is None:
        logger.warning("execute_tool_call unknown tool: %s", name)
        return {"error": f"未知工具: {name}"}
    try:
        return handler(args, ctx)
    except Exception as err:
        logger.exception("tool handler failed tool=%s error=%s", name, err)
        return {"error": str(err)}
