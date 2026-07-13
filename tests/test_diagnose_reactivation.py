from datetime import datetime
from unittest.mock import patch


def _create_account(account_id: str, *, business_day: str = "2026-06-05") -> int:
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


from tests.factories import create_route as _create_route


def _insert_message(
    *,
    account_id: str,
    session_id: int,
    text: str,
    role: str = "user",
    created_at: str = "2026-06-05 09:00:00",
) -> None:
    from app.db import connect, insert_message

    row_id = insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=f"{account_id}-{role}-{created_at}",
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


def test_recent_chat_summary_is_account_isolated(fresh_db):
    from scripts.diagnose_reactivation import recent_chat_summary

    session_a = _create_account("acc-react-diag-a")
    session_b = _create_account("acc-react-diag-b")
    _insert_message(
        account_id="acc-react-diag-a",
        session_id=session_a,
        text="昨天那个相亲对象后来有回复。",
        created_at="2026-06-05 08:00:00",
    )
    _insert_message(
        account_id="acc-react-diag-a",
        session_id=session_a,
        text="听起来你还挺纠结。",
        role="assistant",
        created_at="2026-06-05 08:01:00",
    )
    _insert_message(
        account_id="acc-react-diag-b",
        session_id=session_b,
        text="另一个账号的聊天不能出现。",
        created_at="2026-06-05 08:02:00",
    )

    summary = recent_chat_summary(
        account_id="acc-react-diag-a",
        now=datetime(2026, 6, 5, 12, 0),
        sample_limit=5,
    )

    assert summary["total"] == 2
    assert summary["by_role"] == {"user": 1, "assistant": 1}
    assert "相亲对象" in summary["samples"][0]["content"]
    assert all("另一个账号" not in item["content"] for item in summary["samples"])


def test_run_planning_check_compacts_unified_candidate(fresh_db):
    from app.proactive.store.account_state import ensure_account_state
    from scripts.diagnose_reactivation import run_planning_check

    _create_account("acc-react-plan")
    ensure_account_state(account_id="acc-react-plan")

    def fake_topic_generator(*, account_id, now):
        return {
            "action": "topic_followup_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-topic-1",
                "type": "topic_followup",
                "text": "昨天那个相亲对象后来有再找你吗？",
                "topic": "相亲后续",
                "generated_at": "2026-06-05 10:00:00",
            },
            "evaluated_at": "2026-06-05 10:00:00",
        }

    result = run_planning_check(
        account_id="acc-react-plan",
        now=datetime(2026, 6, 5, 10, 0),
        topic_followup_generator=fake_topic_generator,
        content_invitation_generator=lambda **kwargs: {"action": "no_op", "reason": "unused"},
    )

    assert result["action"] == "reactivation_candidate_planned"
    assert result["reactivation_type"] == "topic_followup"
    assert result["reactivation_candidate"]["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert result["reactivation_candidate"]["scheduled_at"] == "2026-06-05 12:15:00"


def test_dispatch_dry_run_does_not_create_outbound_rows(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.store.candidates import upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state
    from scripts.diagnose_reactivation import run_dispatch_dry_run

    _create_account("acc-react-dispatch-diag")
    _create_route("acc-react-dispatch-diag")
    ensure_account_state(account_id="acc-react-dispatch-diag")
    upsert_reactivation_candidate(
        account_id="acc-react-dispatch-diag",
        candidate={
            "id": "react-diag-1",
            "type": "topic_followup",
            "text": "昨晚小家伙睡得乖不乖？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    result = run_dispatch_dry_run(
        account_id="acc-react-dispatch-diag",
        now=datetime(2026, 6, 5, 12, 15),
    )

    assert result["action"] == "would_send"
    assert result["text"] == "昨晚小家伙睡得乖不乖？"
    assert result["created_outbound_rows"] == 0
    assert list_outbound_messages(account_id="acc-react-dispatch-diag") == []


def test_temporary_database_copy_isolates_candidate_writes(fresh_db):
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from scripts.diagnose_reactivation import temporary_database_copy

    _create_account("acc-react-temp")
    original_path = fresh_db.database_path

    with patch("scripts.diagnose_reactivation.settings", fresh_db):
        with temporary_database_copy() as temp_path:
            assert temp_path != original_path
            upsert_reactivation_candidate(
                account_id="acc-react-temp",
                candidate={
                    "id": "react-temp-1",
                    "type": "topic_followup",
                    "text": "昨天的事后来怎么样了？",
                },
            )
            assert get_reactivation_candidate(account_id="acc-react-temp")["id"] == "react-temp-1"

    assert fresh_db.database_path == original_path
    assert get_reactivation_candidate(account_id="acc-react-temp") is None
