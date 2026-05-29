import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.executor")


def execute_tool_call(name: str, args: dict, ctx: "TurnContext") -> dict:
    """Dispatch a tool call to the appropriate handler. Never raises — returns error dict on failure."""
    from app.tools.reminder_handlers import (
        handle_cancel_reminder,
        handle_create_reminder,
        handle_list_reminders,
        handle_update_reminder,
    )

    handlers = {
        "create_reminder": handle_create_reminder,
        "list_reminders": handle_list_reminders,
        "cancel_reminder": handle_cancel_reminder,
        "update_reminder": handle_update_reminder,
    }
    handler = handlers.get(name)
    if handler is None:
        logger.warning("execute_tool_call unknown tool: %s", name)
        return {"error": f"未知工具: {name}"}
    try:
        return handler(args, ctx)
    except Exception as err:
        logger.exception("tool handler failed tool=%s error=%s", name, err)
        return {"error": str(err)}
