from datetime import datetime
from unittest.mock import patch


def _create_account(account_id: str, *, business_day: str = "2026-06-04") -> int:
    from app.db import get_or_create_session

    session = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
        business_day=business_day,
    )
    return int(session["session"]["id"])


def _insert_message(
    *,
    account_id: str,
    session_id: int,
    text: str,
    role: str = "user",
    created_at: str = "2026-06-04 09:00:00",
) -> None:
    from app.db import connect, insert_message

    message_id = f"{account_id}-{role}-{created_at}"
    row_id = insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound" if role == "user" else "outbound",
        role=role,
        message_type="text",
        content=text,
        raw={"source": "test"},
    )
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )


def test_today_chat_summary_is_account_isolated(fresh_db):
    from scripts.diagnose_content_invitations import today_chat_summary

    session_a = _create_account("acc-today-a")
    session_b = _create_account("acc-today-b")
    _insert_message(
        account_id="acc-today-a",
        session_id=session_a,
        text="今天持续关注 AI 产品。",
        role="user",
    )
    _insert_message(
        account_id="acc-today-a",
        session_id=session_a,
        text="可以看看后续。",
        role="assistant",
        created_at="2026-06-04 09:01:00",
    )
    _insert_message(
        account_id="acc-today-b",
        session_id=session_b,
        text="另一个账号的内容不能串进来。",
        role="user",
    )

    summary = today_chat_summary(
        account_id="acc-today-a",
        today="2026-06-04",
        sample_limit=5,
    )

    assert summary["total"] == 2
    assert summary["by_role"] == {"assistant": 1, "user": 1}
    assert "AI 产品" in summary["samples"][0]["content"]
    assert all("另一个账号" not in item["content"] for item in summary["samples"])


def test_bootstrap_proactive_state_only_for_existing_account(fresh_db):
    from app.db import get_proactive_account_state
    from scripts.diagnose_content_invitations import maybe_bootstrap_proactive_state

    with patch("scripts.diagnose_content_invitations.settings", fresh_db):
        missing = maybe_bootstrap_proactive_state(
            account_id="missing-account",
            now=datetime(2026, 6, 4, 10, 0),
        )

        _create_account("acc-bootstrap")
        created = maybe_bootstrap_proactive_state(
            account_id="acc-bootstrap",
            now=datetime(2026, 6, 4, 10, 0),
        )

    assert missing is None
    assert created is not None
    state = get_proactive_account_state(account_id="acc-bootstrap")
    assert state is not None
    assert state["enabled"] is True
    assert state["metadata"]["bootstrap_source"] == "diagnose_content_invitations"


def test_temporary_database_copy_isolates_invitation_writes(fresh_db):
    from app.db import create_content_invitation, list_content_invitations_for_account
    from scripts.diagnose_content_invitations import temporary_database_copy

    _create_account("acc-temp-db")
    original_path = fresh_db.database_path

    with patch("scripts.diagnose_content_invitations.settings", fresh_db):
        with temporary_database_copy() as temp_path:
            assert temp_path != original_path
            create_content_invitation(
                account_id="acc-temp-db",
                topic="AI 产品",
                invitation_text="我看到几条 AI 产品相关标题，要不要发你看看？",
                title_items=[
                    {"title": "标题一", "source_name": "source", "url": "https://example.com/1"},
                    {"title": "标题二", "source_name": "source", "url": "https://example.com/2"},
                    {"title": "标题三", "source_name": "source", "url": "https://example.com/3"},
                ],
                scheduled_at="2026-06-04 10:00:00",
                expires_at="2026-06-05 10:00:00",
            )
            assert len(list_content_invitations_for_account(account_id="acc-temp-db")) == 1

    assert fresh_db.database_path == original_path
    assert list_content_invitations_for_account(account_id="acc-temp-db") == []
