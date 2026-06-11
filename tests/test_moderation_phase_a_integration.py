from unittest.mock import patch

import app.turn_service as turn_service
from app.schemas import OpenClawTurnRequest


def _turn_payload(message_id: str, text: str = "hello") -> dict:
    return {
        "channel_account_id": "chan-mod",
        "account_id": "chan-mod",
        "session_key": "session-mod",
        "sender_id": "sender-mod",
        "chat_id": "sender-mod",
        "chat_type": "private",
        "message_type": "text",
        "message_id": message_id,
        "text": text,
    }


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def test_turn_sync_guard_blocks_original_reply(monkeypatch, fresh_db):
    from app.db import list_content_moderation_tasks, list_recent_messages_for_account
    from app.rate_limiter import RateLimiter

    monkeypatch.setattr(turn_service, "settings", fresh_db)
    monkeypatch.setattr(turn_service, "rate_limiter", RateLimiter())
    monkeypatch.setattr(
        turn_service,
        "generate_reply_with_tools",
        lambda **_: ("unsafe MODERATION_TEST_BLOCK reply", None),
    )

    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_turn_payload("mod-turn-1")))
    account_id = res.metadata["account_id"]
    messages = list_recent_messages_for_account(account_id=account_id, limit=20)
    tasks = list_content_moderation_tasks(account_id=account_id, limit=20)
    blocked_tasks = [task for task in tasks if task["source_type"] == "generated_reply"]

    assert res.reply == fresh_db.moderation_safe_fallback_text
    assert messages[-1]["content"] == fresh_db.moderation_safe_fallback_text
    assert "MODERATION_TEST_BLOCK" not in messages[-1]["content"]
    assert blocked_tasks
    assert blocked_tasks[0]["status"] == "blocked"
    assert "MODERATION_TEST_BLOCK" in blocked_tasks[0]["snapshot_text"]
    # 被拦截的回复只应留下一条 generated_reply 任务（原文）；安全兜底文案不再单独建出站任务。
    outbound_reply_tasks = [
        task
        for task in tasks
        if task["direction"] == "outbound" and task["source_type"] == "message"
    ]
    assert outbound_reply_tasks == []


def test_proactive_sync_guard_cancels_without_gateway_send(fresh_db):
    from app.db import list_content_moderation_tasks
    from app.proactive.messaging import send_proactive_text

    _create_account("acc-proactive-mod")
    with patch("app.proactive.messaging.send_weixin_text") as mock_send:
        row = send_proactive_text(
            account_id="acc-proactive-mod",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-proactive-mod",
            source="account_check",
            text="please send MODERATION_TEST_BLOCK",
            idempotency_key="proactive-mod-1",
            product_category="companion_followup",
        )

    tasks = list_content_moderation_tasks(account_id="acc-proactive-mod", limit=20)
    blocked_tasks = [task for task in tasks if task["source_type"] == "outbound_message"]

    assert row["status"] == "cancelled"
    assert row["error"] == "moderation_sync_blocked"
    assert row["text"] == fresh_db.moderation_blocked_placeholder
    assert mock_send.call_count == 0
    assert blocked_tasks
    assert blocked_tasks[0]["status"] == "blocked"
    assert blocked_tasks[0]["outbound_message_id"] == row["id"]
    assert "MODERATION_TEST_BLOCK" in blocked_tasks[0]["snapshot_text"]
