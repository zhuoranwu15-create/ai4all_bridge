import json
from unittest.mock import MagicMock, patch

import pytest

from app.turn_context import TurnContext


def _make_ctx():
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id="acc-1",
        account={"id": "acc-1"},
        session={"id": 1},
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="测试",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


def _direct_text_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


def _tool_call_response(tool_name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_generate_reply_with_tools_direct_response():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_force_ipv4 = False
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的，我明白了")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="你好",
                history=[],
                system_prompt="你是助手",
                tools=[],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好的，我明白了"
    mock_chat.assert_called_once()


def test_generate_reply_with_tools_tool_call():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_force_ipv4 = False
    settings_mock.llm_default_prompt = "你是助手"

    tool_resp = _tool_call_response("create_reminder", {"text": "开会", "due_at": "2026-06-01 10:00:00"})
    tool_result = {"status": "created", "reminder_id": "rem-1", "due_at": "2026-06-01 10:00:00"}
    final_text = "好的，我会在6月1日上午10点提醒你开会。"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=tool_resp):
            with patch("app.llm._http_chat", return_value=final_text):
                with patch("app.tools.executor.execute_tool_call", return_value=tool_result):
                    reply, err = generate_reply_with_tools(
                        user_text="明天上午10点提醒我开会",
                        history=[],
                        system_prompt="你是助手",
                        tools=[{"type": "function", "function": {"name": "create_reminder"}}],
                        ctx=_make_ctx(),
                    )

    assert err is None
    assert reply == final_text


def test_generate_reply_with_tools_no_api_key_returns_fallback():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = ""

    with patch("app.llm.settings", settings_mock):
        reply, err = generate_reply_with_tools(
            user_text="你好",
            history=[],
            system_prompt=None,
            tools=[],
            ctx=_make_ctx(),
        )

    assert err is None
    assert "你好" in reply
