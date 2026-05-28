from datetime import datetime
from unittest.mock import patch


def _create_account(account_id: str) -> int:
    from app.db import get_or_create_session

    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    return int(state["session"]["id"])


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def _create_state(account_id: str) -> None:
    from app.proactive.state import ensure_account_state

    ensure_account_state(
        account_id=account_id,
        enabled=True,
        next_scan_at=datetime(2026, 5, 22, 9, 0),
    )


def test_extract_commitment_from_turn_writes_pending_commitment(fresh_db):
    from app.db import list_proactive_commitments_for_account
    from app.proactive.commitments import extract_commitment_from_turn

    fresh_db.llm_api_key = "fake-key"
    session_id = _create_account("acc-com-extract")
    _create_route("acc-com-extract")
    _create_state("acc-com-extract")

    with (
        patch("app.proactive.commitments.settings", fresh_db),
        patch(
            "app.proactive.commitments.generate_completion",
            return_value=(
                '{"should_create": true, '
                '"text": "明天记得看一下事情 A 的后续 B。", '
                '"due_at": "2026-05-23 09:30:00", '
                '"reason": "用户提到了后续 B", "confidence": 0.95}'
            ),
        ) as mock_generate,
    ):
        result = extract_commitment_from_turn(
            account_id="acc-com-extract",
            session_id=session_id,
            user_text="昨天检查事情 A，明天可能还要看一下后续 B。",
            assistant_text="好，我记下这个后续。",
            source_message_id="msg-com-1",
            source_reply_message_id="reply-com-1",
            now=datetime(2026, 5, 22, 10, 0),
        )
        commitments = list_proactive_commitments_for_account(
            account_id="acc-com-extract"
        )

    assert result["action"] == "created_commitment"
    assert len(commitments) == 1
    assert commitments[0]["status"] == "pending"
    assert commitments[0]["text"] == "明天记得看一下事情 A 的后续 B。"
    assert commitments[0]["due_at"] == "2026-05-23 09:30:00"
    assert commitments[0]["confidence"] == 0.95
    assert commitments[0]["metadata"]["source"] == "hidden_commitment_extractor_v1"
    mock_generate.assert_called_once()
    messages = mock_generate.call_args.args[0]
    assert messages[0]["role"] == "system"
    assert "隐藏 follow-up commitment 抽取器" in messages[0]["content"]
    assert "后续 B" in messages[1]["content"]


def test_extract_commitment_rejects_low_confidence(fresh_db):
    from app.db import list_proactive_commitments_for_account
    from app.proactive.commitments import extract_commitment_from_turn

    fresh_db.llm_api_key = "fake-key"
    session_id = _create_account("acc-com-low")
    _create_route("acc-com-low")
    _create_state("acc-com-low")

    with (
        patch("app.proactive.commitments.settings", fresh_db),
        patch(
            "app.proactive.commitments.generate_completion",
            return_value=(
                '{"should_create": true, "text": "低置信事项", '
                '"due_at": "2026-05-23 09:30:00", '
                '"reason": "不够确定", "confidence": 0.4}'
            ),
        ),
    ):
        result = extract_commitment_from_turn(
            account_id="acc-com-low",
            session_id=session_id,
            user_text="只是普通聊天。",
            assistant_text="嗯嗯。",
            source_message_id="msg-low",
            source_reply_message_id="reply-low",
            now=datetime(2026, 5, 22, 10, 0),
        )
        commitments = list_proactive_commitments_for_account(account_id="acc-com-low")

    assert result["action"] == "no_op"
    assert result["reason"] == "llm_no_commitment"
    assert commitments == []


def test_dispatch_due_commitment_sends_once(fresh_db):
    from app.db import (
        create_proactive_commitment,
        get_proactive_commitment,
        list_outbound_messages,
    )
    from app.proactive.commitments import dispatch_due_commitments
    from app.proactive.state import get_account_state

    fresh_db.proactive_outbound_daily_limit = 3
    session_id = _create_account("acc-com-dispatch")
    _create_route("acc-com-dispatch")
    _create_state("acc-com-dispatch")
    create_proactive_commitment(
        commitment_id="com-dispatch",
        account_id="acc-com-dispatch",
        session_id=session_id,
        source_message_id="msg-dispatch",
        source_reply_message_id="reply-dispatch",
        text="关注一下事情 B 的后续。",
        due_at="2026-05-22 09:30:00",
        confidence=0.96,
        reason="测试 due commitment",
    )

    with (
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:commitment-1"},
        ) as mock_send,
    ):
        result = dispatch_due_commitments(
            now=datetime(2026, 5, 22, 10, 0),
            limit=10,
        )
        commitment = get_proactive_commitment(commitment_id="com-dispatch")
        outbound = list_outbound_messages(account_id="acc-com-dispatch")
        state = get_account_state(account_id="acc-com-dispatch")

    assert result[0]["status"] == "sent"
    assert commitment["status"] == "sent"
    assert commitment["outbound_message_id"] == outbound[0]["id"]
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["source"] == "commitment"
    assert outbound[0]["idempotency_key"] == "commitment-com-dispatch"
    assert outbound[0]["metadata"]["commitment_id"] == "com-dispatch"
    assert state["last_proactive_sent_at"] == "2026-05-22 10:00:00"
    mock_send.assert_called_once_with(
        to_user_id="user@im.wechat",
        text="关注一下事情 B 的后续。",
        gateway_timeout_ms=fresh_db.openclaw_gateway_call_timeout_ms,
        account_id="bot-1",
        idempotency_key="commitment-com-dispatch",
        session_key="session-acc-com-dispatch",
        channel="openclaw-weixin",
    )


def test_due_commitment_requires_enabled_proactive_state(fresh_db):
    from app.db import create_proactive_commitment, list_due_proactive_commitments

    _create_account("acc-com-disabled")
    _create_route("acc-com-disabled")
    create_proactive_commitment(
        commitment_id="com-disabled",
        account_id="acc-com-disabled",
        text="不应发送。",
        due_at="2026-05-22 09:30:00",
        confidence=0.96,
    )

    due = list_due_proactive_commitments(
        now="2026-05-22 10:00:00",
        limit=10,
    )

    assert due == []
