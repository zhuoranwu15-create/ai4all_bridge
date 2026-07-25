"""Handler for the session_status tool: relationship/session facts for one account.

Returns only relationship-flavored facts (认识天数 / 首聊日期 / 连续聊天天数). Time and
date come from the system prompt runtime block, not here; internal runtime metrics are
deliberately not exposed to the companion model.
"""
import logging
from datetime import date, timedelta
from typing import List, Optional, TYPE_CHECKING

from app.db import get_first_user_message_at, list_user_active_dates
from app.time_utils import beijing_now

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext

logger = logging.getLogger("ai4all.tools.session_status_handlers")


def _parse_date(d: str) -> date:
    y, m, day = (int(p) for p in d.split("-"))
    return date(y, m, day)


def compute_streak(active_dates: List[str], today: str) -> int:
    """Length of the consecutive-day run ending at *today* (or, if today has no
    activity yet, at the most recent active date). 0 when there is no activity.

    *active_dates* are distinct 'YYYY-MM-DD' strings; order does not matter.
    """
    if not active_dates:
        return 0
    active = set(active_dates)
    anchor = today if today in active else max(active_dates)
    cursor = _parse_date(anchor)
    streak = 0
    while cursor.isoformat() in active:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def handle_session_status(args: dict, ctx: "TurnContext") -> dict:
    """Return relationship facts for the current account. Account-scoped; never raises
    meaningfully (executor wraps exceptions)."""
    account_id = ctx.account_id
    today = beijing_now().date()
    today_str = today.isoformat()

    first_at: Optional[str] = get_first_user_message_at(account_id=account_id)
    active_dates = list_user_active_dates(account_id=account_id)

    if not first_at:
        # Brand-new account with no inbound message recorded yet.
        return {
            "status": "ok",
            "first_chat_date": None,
            "days_known": 0,
            "chat_streak_days": 0,
        }

    first_date = _parse_date(first_at[:10])
    days_known = (today - first_date).days  # elapsed days; 0 means met today

    return {
        "status": "ok",
        "first_chat_date": first_date.isoformat(),
        "days_known": days_known,
        "chat_streak_days": compute_streak(active_dates, today_str),
    }
