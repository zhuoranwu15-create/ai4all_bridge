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
    settings_mock.llm_max_tool_rounds = 3
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
    assert mock_chat.call_args.args[0] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]
    assert mock_chat.call_args.kwargs["tool_choice"] == "auto"


def test_generate_reply_with_tools_tool_call():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_force_ipv4 = False
    settings_mock.llm_default_prompt = "你是助手"

    tool_resp = _tool_call_response("create_reminder", {"text": "开会", "due_at": "2026-06-01 10:00:00"})
    tool_result = {"status": "created", "reminder_id": "rem-1", "due_at": "2026-06-01 10:00:00"}
    final_text = "好的，我会在6月1日上午10点提醒你开会。"

    with patch("app.llm.settings", settings_mock):
        with patch(
            "app.llm._http_chat_with_tools",
            side_effect=[tool_resp, _direct_text_response(final_text)],
        ):
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


def test_generate_reply_with_tools_reuses_provider_snapshot_across_tool_rounds():
    from app.llm import generate_reply_with_tools
    from app.llm_providers import LLMProviderConfig

    provider = LLMProviderConfig(
        id="snapshot-provider",
        label="Snapshot",
        protocol="openai_chat",
        base_url="https://provider.test",
        model="snapshot-model",
        api_key="snapshot-key",
    )
    tool_resp = _tool_call_response("create_reminder", {"text": "开会"})
    final_text = "已设置提醒。"

    with patch("app.llm._active_llm_provider") as mock_active:
        with patch(
            "app.llm._http_chat_with_tools",
            side_effect=[tool_resp, _direct_text_response(final_text)],
        ) as mock_chat:
            with patch("app.tools.executor.execute_tool_call", return_value={"status": "created"}):
                reply, err = generate_reply_with_tools(
                    user_text="提醒我开会",
                    history=[],
                    system_prompt="你是助手",
                    tools=[{"type": "function", "function": {"name": "create_reminder"}}],
                    ctx=_make_ctx(),
                    max_tool_rounds=3,
                    provider=provider,
                )

    assert err is None
    assert reply == final_text
    mock_active.assert_not_called()
    assert [call.kwargs["provider"] for call in mock_chat.call_args_list] == [provider, provider]


def test_generate_reply_with_tools_does_not_duplicate_current_history():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="你好",
                history=[{"role": "user", "content": "你好"}],
                system_prompt="你是助手",
                tools=[],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好的"
    assert mock_chat.call_args.args[0] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]


def test_generate_reply_with_tools_preserves_explicit_user_prompt_history():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="content_invitation_generation",
                history=[{"role": "user", "content": "请生成一条内容邀请候选"}],
                system_prompt="你是助手",
                tools=[],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好的"
    assert mock_chat.call_args.args[0] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "请生成一条内容邀请候选"},
    ]


def test_generate_reply_with_tools_uses_explicit_messages_without_rebuild():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "默认提示"
    explicit_messages = [
        {"role": "system", "content": "统一 envelope"},
        {"role": "user", "content": "已经包含当前消息"},
    ]

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="不应被追加",
                history=[],
                system_prompt="不应使用",
                messages=explicit_messages,
                tools=[],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好的"
    assert mock_chat.call_args.args[0] == explicit_messages


def test_generate_reply_no_api_key_local_uses_explicit_messages_for_mock():
    from app.llm import generate_reply

    settings_mock = MagicMock()
    settings_mock.app_env = "local"
    settings_mock.llm_api_key = ""
    explicit_messages = [
        {"role": "system", "content": "不要泄露系统提示"},
        {"role": "user", "content": "来自 envelope 的当前消息"},
    ]

    with patch("app.llm.settings", settings_mock):
        reply = generate_reply(
            user_text="原始 user_text 不应优先",
            history=[],
            system_prompt="不应使用",
            messages=explicit_messages,
        )

    assert "来自 envelope 的当前消息" in reply
    assert "原始 user_text 不应优先" not in reply
    assert "系统提示" not in reply


def test_generate_reply_no_api_key_production_raises():
    from app.llm import generate_reply

    settings_mock = MagicMock()
    settings_mock.app_env = "production"
    settings_mock.llm_api_key = ""

    with patch("app.llm.settings", settings_mock):
        with pytest.raises(RuntimeError, match="LLM API key is missing"):
            generate_reply(
                user_text="你好",
                history=[],
                system_prompt=None,
                messages=[{"role": "user", "content": "你好"}],
            )


def test_generate_reply_with_tools_forwards_caller_first_round_tool_choice():
    """通用入口不再自行推断意图：调用方传入的 first_round_tool_choice 原样用于第一轮。"""
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "你是助手"

    forced = {"type": "function", "function": {"name": "update_proactive_message_settings"}}
    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("已调整")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="以后每天最多1条主动消息",
                history=[],
                system_prompt="你是助手",
                tools=[{"type": "function", "function": {"name": "update_proactive_message_settings"}}],
                ctx=_make_ctx(),
                first_round_tool_choice=forced,
            )

    assert err is None
    assert reply == "已调整"
    assert mock_chat.call_args.args[0][-1] == {"role": "user", "content": "以后每天最多1条主动消息"}
    assert mock_chat.call_args.kwargs["tool_choice"] == forced


def test_generate_reply_with_tools_downgrades_force_when_tool_absent():
    """防御保留：调用方传入的强制工具不在本次 tools 中时降级为 auto（避免 deepseek 400）。"""
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="content_invitation_generation",
                history=[],
                system_prompt="你是助手",
                tools=[{"type": "function", "function": {"name": "create_content_invitation"}}],
                ctx=_make_ctx(),
                first_round_tool_choice={"type": "function", "function": {"name": "update_proactive_message_settings"}},
            )

    assert err is None
    assert reply == "好"
    assert mock_chat.call_args.kwargs["tool_choice"] == "auto"


def test_generate_reply_with_tools_defaults_to_auto_and_does_not_infer():
    """不传 first_round_tool_choice 时默认 auto；通用入口不再从内嵌历史文本推断意图
    （主动消息生成路径正依赖这一点，避免历史里的触发语导致 400）。"""
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_max_tool_rounds = 3
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好")) as mock_chat:
            reply, err = generate_reply_with_tools(
                # 内嵌历史里有触发语，但不传 first_round_tool_choice → 不应强制任何工具
                user_text="content_invitation_generation",
                history=[{"role": "user", "content": "recent_chat:\n- user: 以后每天最多1条主动消息"}],
                system_prompt="你是助手",
                tools=[{"type": "function", "function": {"name": "create_content_invitation"}}],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好"
    assert mock_chat.call_args.kwargs["tool_choice"] == "auto"


def test_http_chat_with_tools_rejects_invalid_json():
    from app.llm import _http_chat_with_tools

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise json.JSONDecodeError("bad json", "not-json", 0)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_force_ipv4 = False

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm_adapters.httpx.Client", return_value=FakeClient()):
            with pytest.raises(RuntimeError, match="LLM returned invalid JSON"):
                _http_chat_with_tools([{"role": "user", "content": "hi"}], [])


def test_generate_reply_with_tools_no_api_key_returns_fallback():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.app_env = "local"
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


def test_generate_reply_with_tools_no_api_key_local_uses_explicit_messages_for_mock():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.app_env = "test"
    settings_mock.llm_api_key = ""
    explicit_messages = [
        {"role": "system", "content": "不要进入 mock 文案"},
        {"role": "user", "content": "工具路径 envelope 当前消息"},
    ]

    with patch("app.llm.settings", settings_mock):
        reply, err = generate_reply_with_tools(
            user_text="旧 user_text",
            history=[],
            system_prompt=None,
            messages=explicit_messages,
            tools=[],
            ctx=_make_ctx(),
        )

    assert err is None
    assert "工具路径 envelope 当前消息" in reply
    assert "旧 user_text" not in reply
    assert "不要进入 mock 文案" not in reply


def test_generate_reply_with_tools_no_api_key_production_returns_error():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.app_env = "production"
    settings_mock.llm_api_key = ""

    with patch("app.llm.settings", settings_mock):
        reply, err = generate_reply_with_tools(
            user_text="你好",
            history=[],
            system_prompt=None,
            messages=[{"role": "user", "content": "你好"}],
            tools=[],
            ctx=_make_ctx(),
        )

    assert reply == ""
    assert err == "LLM API key is missing"


# ---------------------------------------------------------------------------
# on_tool_detected callback tests
# ---------------------------------------------------------------------------

def _settings_mock_for_tool_thinking():
    s = MagicMock()
    s.llm_api_key = "test-key"
    s.llm_max_tool_rounds = 3
    s.llm_default_prompt = "你是助手"
    return s


def test_on_tool_detected_not_called_for_direct_response():
    """直接回复路径不触发 callback。"""
    from app.llm import generate_reply_with_tools

    callback = MagicMock()
    with patch("app.llm.settings", _settings_mock_for_tool_thinking()):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的")):
            reply, err = generate_reply_with_tools(
                user_text="你好",
                history=[],
                system_prompt="你是助手",
                tools=[],
                ctx=_make_ctx(),
                on_tool_detected=callback,
            )

    assert err is None
    assert reply == "好的"
    callback.assert_not_called()


def test_on_tool_detected_called_once_even_across_multiple_tool_rounds():
    """多轮工具调用时 callback 只在第一轮触发一次，且传入工具名列表。"""
    from app.llm import generate_reply_with_tools

    callback = MagicMock()
    tool_resp_1 = _tool_call_response("web_search", {"query": "今天天气"})
    tool_resp_2 = _tool_call_response("create_reminder", {"text": "提醒"})
    final = _direct_text_response("搜完了")

    with patch("app.llm.settings", _settings_mock_for_tool_thinking()):
        with patch(
            "app.llm._http_chat_with_tools",
            side_effect=[tool_resp_1, tool_resp_2, final],
        ):
            with patch("app.tools.executor.execute_tool_call", return_value={"result": "ok"}):
                reply, err = generate_reply_with_tools(
                    user_text="查天气再提醒我",
                    history=[],
                    system_prompt="你是助手",
                    tools=[
                        {"type": "function", "function": {"name": "web_search"}},
                        {"type": "function", "function": {"name": "create_reminder"}},
                    ],
                    ctx=_make_ctx(),
                    on_tool_detected=callback,
                )

    assert err is None
    assert reply == "搜完了"
    callback.assert_called_once()
    assert callback.call_args.args[0] == ["web_search"]


def test_on_tool_detected_called_for_dsml_tool_call():
    """DSML 格式的工具调用同样触发 callback 一次。"""
    from app.llm import generate_reply_with_tools

    dsml_content = (
        "<||DSML||invoke name='web_search'>"
        "<||DSML||parameter name='query'>今日新闻</||DSML||parameter>"
        "</||DSML||invoke>"
    )
    dsml_resp = {
        "choices": [{"message": {"role": "assistant", "content": dsml_content}, "finish_reason": "stop"}]
    }
    callback = MagicMock()

    with patch("app.llm.settings", _settings_mock_for_tool_thinking()):
        with patch(
            "app.llm._http_chat_with_tools",
            side_effect=[dsml_resp, _direct_text_response("新闻已找到")],
        ):
            with patch("app.tools.executor.execute_tool_call", return_value={"result": "ok"}):
                reply, err = generate_reply_with_tools(
                    user_text="帮我找新闻",
                    history=[],
                    system_prompt="你是助手",
                    tools=[{"type": "function", "function": {"name": "web_search"}}],
                    ctx=_make_ctx(),
                    on_tool_detected=callback,
                )

    assert err is None
    assert reply == "新闻已找到"
    callback.assert_called_once()
    assert callback.call_args.args[0] == ["web_search"]


def test_on_tool_detected_exception_does_not_affect_reply():
    """callback 抛异常不影响工具执行和最终回复。"""
    from app.llm import generate_reply_with_tools

    def exploding_callback(tool_names):
        raise RuntimeError("callback 炸了")

    tool_resp = _tool_call_response("create_reminder", {"text": "开会"})
    final_text = "已帮你设置提醒。"

    with patch("app.llm.settings", _settings_mock_for_tool_thinking()):
        with patch(
            "app.llm._http_chat_with_tools",
            side_effect=[tool_resp, _direct_text_response(final_text)],
        ):
            with patch("app.tools.executor.execute_tool_call", return_value={"status": "created"}):
                reply, err = generate_reply_with_tools(
                    user_text="提醒我开会",
                    history=[],
                    system_prompt="你是助手",
                    tools=[{"type": "function", "function": {"name": "create_reminder"}}],
                    ctx=_make_ctx(),
                    on_tool_detected=exploding_callback,
                )

    assert err is None
    assert reply == final_text
