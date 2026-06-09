from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.time_utils import beijing_now, beijing_daypart_str, beijing_weekday_str
from app.tools.session_status_handlers import compute_streak, handle_session_status

_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# Pure helpers (no DB)
# --------------------------------------------------------------------------

def test_compute_streak_consecutive_ending_today():
    assert compute_streak(["2026-06-07", "2026-06-08", "2026-06-09"], "2026-06-09") == 3


def test_compute_streak_gap_breaks_run():
    # 06-06 is isolated; run ending today is only 08+09
    assert compute_streak(["2026-06-06", "2026-06-08", "2026-06-09"], "2026-06-09") == 2


def test_compute_streak_today_inactive_falls_back_to_latest_active():
    assert compute_streak(["2026-06-07", "2026-06-08"], "2026-06-09") == 2


def test_compute_streak_empty():
    assert compute_streak([], "2026-06-09") == 0


def test_weekday_labels():
    assert beijing_weekday_str(datetime(2026, 6, 9, 12, tzinfo=_TZ)) == "周二"
    assert beijing_weekday_str(datetime(2026, 6, 14, 12, tzinfo=_TZ)) == "周日"


def test_daypart_buckets():
    assert beijing_daypart_str(datetime(2026, 6, 9, 6, tzinfo=_TZ)) == "清晨"
    assert beijing_daypart_str(datetime(2026, 6, 9, 9, tzinfo=_TZ)) == "上午"
    assert beijing_daypart_str(datetime(2026, 6, 9, 12, tzinfo=_TZ)) == "中午"
    assert beijing_daypart_str(datetime(2026, 6, 9, 15, tzinfo=_TZ)) == "下午"
    assert beijing_daypart_str(datetime(2026, 6, 9, 18, tzinfo=_TZ)) == "傍晚"
    assert beijing_daypart_str(datetime(2026, 6, 9, 21, tzinfo=_TZ)) == "晚上"
    assert beijing_daypart_str(datetime(2026, 6, 9, 2, tzinfo=_TZ)) == "深夜"


# --------------------------------------------------------------------------
# Handler + DB helpers (account-scoped)
# --------------------------------------------------------------------------

def _session(account_id: str) -> int:
    from app.db import get_or_create_session

    s = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"openclaw-weixin:{account_id}:sender",
        carryover_summary=None,
    )
    return int(s["session"]["id"])


def _seed_user_message(account_id: str, session_id: int, day: str, idx: int) -> None:
    """Insert one inbound user message with an explicit created_at date."""
    from app.db import connect

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO messages(
                account_id, session_id, message_id, reply_to_message_id,
                direction, role, message_type, content, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, session_id, f"m-{account_id}-{idx}", None,
                "inbound", "user", "text", "hi", "{}", f"{day} 09:00:00",
            ),
        )


def test_handle_session_status_relationship_and_streak(fresh_db):
    today = beijing_now().date()
    sid = _session("acctA")
    # First chat 10 days ago (isolated), then a 3-day consecutive run ending today.
    days = [today - timedelta(days=10), today - timedelta(days=2),
            today - timedelta(days=1), today]
    for i, d in enumerate(days):
        _seed_user_message("acctA", sid, d.isoformat(), i)

    out = handle_session_status({}, SimpleNamespace(account_id="acctA"))

    assert out["status"] == "ok"
    assert out["first_chat_date"] == (today - timedelta(days=10)).isoformat()
    assert out["days_known"] == 10
    assert out["chat_streak_days"] == 3  # 10-days-ago entry is isolated, does not extend


def test_handle_session_status_account_isolation(fresh_db):
    today = beijing_now().date()
    sa = _session("A")
    sb = _session("B")
    _seed_user_message("A", sa, today.isoformat(), 0)
    _seed_user_message("B", sb, (today - timedelta(days=5)).isoformat(), 0)
    _seed_user_message("B", sb, today.isoformat(), 1)

    out_a = handle_session_status({}, SimpleNamespace(account_id="A"))
    out_b = handle_session_status({}, SimpleNamespace(account_id="B"))

    # A only knows about its own single message today.
    assert out_a["first_chat_date"] == today.isoformat()
    assert out_a["days_known"] == 0
    assert out_a["chat_streak_days"] == 1
    # B is unaffected by A.
    assert out_b["first_chat_date"] == (today - timedelta(days=5)).isoformat()
    assert out_b["days_known"] == 5


def test_handle_session_status_brand_new_account(fresh_db):
    out = handle_session_status({}, SimpleNamespace(account_id="ghost"))
    assert out == {
        "status": "ok",
        "first_chat_date": None,
        "days_known": 0,
        "chat_streak_days": 0,
    }
