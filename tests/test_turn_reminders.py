"""
End-to-end tests for reminder creation via the /openclaw/turn endpoint
using LLM tool use. The LLM is mocked to return a tool_calls response.
"""
from unittest.mock import patch

import pytest


def test_turn_creates_reminder_via_tool_call(client, fresh_db):
    """When LLM returns create_reminder tool call, reply is returned correctly."""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    # Set up account with completed onboarding so tool path fires
    with _patch("app.db.settings", fresh_db):
        session = get_or_create_session(
            account_id="test-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="test-session",
        )
        set_account_onboarding_state(account_id="test-session", state="complete")

    tool_call_response = (
        "好的，我会在2026年6月1日上午10点提醒你检查事情A。",
        None,
    )

    with patch("app.turn_service.generate_reply_with_tools", return_value=tool_call_response) as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "明天上午10点提醒我检查事情A",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    mock_llm.assert_called_once()
    assert "提醒" in data.get("reply", "")


def test_turn_tool_generation_error_returns_friendly_reply(client, fresh_db, caplog):
    """Tool LLM errors should not persist or return an empty assistant message."""
    import logging
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, list_sessions_for_account, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="tool-error-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="tool-error-session",
        )
        set_account_onboarding_state(account_id="tool-error-session", state="complete")

    with caplog.at_level(logging.ERROR, logger="ai4all.turn_service"):
        with patch(
            "app.turn_service.generate_reply_with_tools",
            return_value=("", "LLM API key is missing"),
        ):
            resp = client.post(
                "/openclaw/turn",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "session_key": "tool-error-session",
                    "channel": "openclaw-weixin",
                    "channel_account_id": "bot-1",
                    "sender_id": "user-1",
                    "chat_id": "chat-1",
                    "message_type": "text",
                    "text": "你好",
                    "chat_type": "private",
                },
            )

    assert resp.status_code == 200
    assert resp.json()["reply"] == "我这边刚刚有点卡住了，你可以稍后再发我一次。"
    session = list_sessions_for_account(account_id="tool-error-session", limit=1)[0]
    assert session["turn_count"] == 0
    # 用户收到「卡住了」兜底回复时须打 ERROR(带 generation_error 详情)→ 进飞书告警管道。
    fallback_alerts = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.ERROR
        and "user received generation fallback reply" in rec.getMessage()
    ]
    assert fallback_alerts, "用户收到卡住了兜底回复应记录 ERROR 告警"
    assert "LLM API key is missing" in fallback_alerts[0].getMessage()


def test_turn_always_exposes_web_search_tool(client, fresh_db):
    """web_search 已转正为默认能力（app.config.WEB_SEARCH_ENABLED 常开），主 turn 恒暴露该工具。"""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="search-gate-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="search-gate-session",
        )
        set_account_onboarding_state(account_id="search-gate-session", state="complete")

    def post_turn():
        return client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "search-gate-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "查一下今天有什么新闻",
                "chat_type": "private",
            },
        )

    with patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)) as mock_llm:
        resp = post_turn()
    assert resp.status_code == 200
    tool_names = {t["function"]["name"] for t in mock_llm.call_args.kwargs["tools"]}
    assert "web_search" in tool_names


def test_tool_turn_final_reply_is_sent_out_of_band(client, fresh_db):
    """工具回合最终回复主动发送，避免 OpenClaw 同步 response 超时后丢最终答案。"""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="tool-final-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="tool-final-session",
        )
        set_account_onboarding_state(account_id="tool-final-session", state="complete")

    def fake_generate(**kwargs):
        kwargs["on_tool_detected"](["web_search"])
        return "搜索后的最终答案", None

    with patch("app.turn_service.generate_reply_with_tools", side_effect=fake_generate), \
        patch("app.turn_service.node_gateway.node_send_text", return_value={"messageId": "gw-final"}) as mock_send:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "tool-final-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "帮我搜索一下今天新闻",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["no_reply"] is True
    assert data["reply"] is None
    assert data["metadata"]["delivery_mode"] == "out_of_band_tool_final"
    assert data["metadata"]["gateway_message_id"] == "gw-final"
    mock_send.assert_called_once()
    send_kwargs = mock_send.call_args.kwargs
    assert send_kwargs["text"] == "搜索后的最终答案"
    assert send_kwargs["to_user_id"] == "chat-1"
    assert send_kwargs["account_id"] == "bot-1"
    assert send_kwargs["idempotency_key"].startswith("tool-final-tool-final-session-reply-")


def test_plain_turn_final_reply_stays_sync(client, fresh_db):
    """没有实际触发工具时，普通聊天仍用同步 reply 返回，不额外主动发送。"""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="plain-final-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="plain-final-session",
        )
        set_account_onboarding_state(account_id="plain-final-session", state="complete")

    with patch("app.turn_service.generate_reply_with_tools", return_value=("普通答案", None)), \
        patch("app.turn_service.node_gateway.node_send_text") as mock_send:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "plain-final-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "普通聊天",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["no_reply"] is False
    assert data["reply"] == "普通答案"
    mock_send.assert_not_called()


def test_turn_forwards_proactive_update_tool_choice(client, fresh_db):
    """主对话 turn 路径：命中主动设置更新意图时向 LLM 显式传入强制 tool_choice；否则 auto。
    锁定 P1-5 的接线（意图判定已从通用 LLM 入口移回 turn 路径）。"""
    from unittest.mock import patch as _patch
    from app.db import get_or_create_session, set_account_onboarding_state

    with _patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="intent-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="intent-session",
        )
        set_account_onboarding_state(account_id="intent-session", state="complete")

    def post_turn(text):
        return client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "intent-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": text,
                "chat_type": "private",
            },
        )

    with patch("app.turn_service.generate_reply_with_tools", return_value=("ok", None)) as mock_llm:
        assert post_turn("以后每天最多1条主动消息").status_code == 200
    assert mock_llm.call_args.kwargs["first_round_tool_choice"] == {
        "type": "function",
        "function": {"name": "update_proactive_message_settings"},
    }

    with patch("app.turn_service.generate_reply_with_tools", return_value=("ok", None)) as mock_llm:
        assert post_turn("今天天气真不错").status_code == 200
    assert mock_llm.call_args.kwargs["first_round_tool_choice"] == "auto"


@pytest.mark.slow
def test_turn_command_bypasses_llm(client, fresh_db):
    """#重置会话 special command does not call generate_reply_with_tools."""
    with patch("app.turn_service.generate_reply_with_tools") as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "#重置会话",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    mock_llm.assert_not_called()
