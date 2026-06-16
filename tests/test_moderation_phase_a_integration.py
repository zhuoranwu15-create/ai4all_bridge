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


def test_turn_inbound_aliyun_block_stops_reply(monkeypatch, fresh_db):
    """入站云审核命中：不调用主模型，返回固定安全话术，入站任务进 needs_review。"""

    from app.db import list_content_moderation_tasks, list_recent_messages_for_account
    from app.moderation.models import MachineReviewResult
    from app.rate_limiter import RateLimiter

    fresh_db.moderation_aliyun_enabled = True
    monkeypatch.setattr(turn_service, "settings", fresh_db)
    monkeypatch.setattr(turn_service, "rate_limiter", RateLimiter())

    def _must_not_call(**_):
        raise AssertionError("generate_reply_with_tools should not run when inbound is blocked")

    monkeypatch.setattr(turn_service, "generate_reply_with_tools", _must_not_call)

    cloud_block = MachineReviewResult(
        reviewer_type="cloud",
        engine="aliyun_text_moderation_plus",
        engine_version="chat_detection_pro",
        level="block",
        categories=["cloud:pornographic_adult", "cat:sexual_content"],
    )
    monkeypatch.setattr(
        "app.moderation.service.aliyun_review.review_text_with_aliyun",
        lambda **_: cloud_block,
    )

    risky_text = "一条命中云审核的入站内容"
    res = turn_service.handle_openclaw_turn(
        OpenClawTurnRequest(**_turn_payload("mod-inbound-block-1", text=risky_text))
    )
    account_id = res.metadata["account_id"]
    tasks = list_content_moderation_tasks(account_id=account_id, limit=20)
    inbound_tasks = [t for t in tasks if t["direction"] == "inbound" and t["source_type"] == "message"]

    assert res.reply == fresh_db.moderation_inbound_blocked_reply_text
    assert inbound_tasks
    assert inbound_tasks[0]["status"] == "needs_review"
    assert inbound_tasks[0]["risk_level"] == "block"
    assert "cat:sexual_content" in inbound_tasks[0]["risk_categories"]

    # 命中原文已打审核标记：不应再进入后续 LLM 上下文（避免下一轮被重新喂给模型）。
    context = list_recent_messages_for_account(account_id=account_id, limit=20)
    assert all(risky_text not in (m["content"] or "") for m in context)


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
            # 本用例只验证 moderation sync guard,与静默时段无关;
            # 显式 bypass 以避免依赖运行时墙钟(夜间会先被 quiet_hours 短路)。
            bypass_quiet_hours=True,
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
