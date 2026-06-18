from unittest.mock import patch

from app.llm_providers import LLMProviderConfig


def _provider(protocol: str, *, base_url: str = "https://provider.test", max_retries: int = 0) -> LLMProviderConfig:
    return LLMProviderConfig(
        id="p1",
        label="Provider",
        protocol=protocol,
        base_url=base_url,
        model="model-1",
        api_key="key-1",
        force_ipv4=False,
        max_retries=max_retries,
    )


def _tool_schema():
    return [
        {
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": "create reminder",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]


def test_openai_responses_adapter_converts_tools_and_function_calls():
    from app.llm_adapters import chat_completion

    provider = _provider("openai_responses")
    with patch(
        "app.llm_adapters._post_json",
        return_value={
            "id": "resp_1",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "create_reminder",
                    "arguments": "{\"text\":\"开会\"}",
                }
            ],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        },
    ) as mock_post:
        payload = chat_completion(
            provider,
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            tools=_tool_schema(),
            tool_choice={"type": "function", "function": {"name": "create_reminder"}},
        )

    body = mock_post.call_args.kwargs["body"]
    assert body["input"][0] == {"role": "developer", "content": "sys"}
    assert body["tools"][0]["name"] == "create_reminder"
    assert body["tool_choice"] == {"type": "function", "name": "create_reminder"}
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["id"] == "call_1"
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}


def test_openai_responses_adapter_uses_previous_response_for_tool_results():
    from app.llm_adapters import chat_completion

    provider = _provider("openai_responses")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "_provider_response_id": "resp_1",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "create_reminder", "arguments": "{\"text\":\"开会\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"status\":\"ok\"}"},
    ]
    with patch(
        "app.llm_adapters._post_json",
        return_value={
            "id": "resp_2",
            "output_text": "已创建",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "已创建"}]}],
        },
    ) as mock_post:
        payload = chat_completion(provider, messages, tools=_tool_schema())

    body = mock_post.call_args.kwargs["body"]
    assert body["previous_response_id"] == "resp_1"
    assert body["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "{\"status\":\"ok\"}"}
    ]
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_openai_responses_url_keeps_existing_version_path():
    from app.llm_adapters import chat_completion

    provider = _provider("openai_responses", base_url="https://proxy.example.com/openai/v2")
    with patch(
        "app.llm_adapters._post_json",
        return_value={"id": "resp_1", "output_text": "ok", "output": []},
    ) as mock_post:
        chat_completion(provider, [{"role": "user", "content": "hi"}])

    assert mock_post.call_args.kwargs["url"] == "https://proxy.example.com/openai/v2/responses"


def test_anthropic_messages_url_keeps_existing_version_path():
    from app.llm_adapters import chat_completion

    provider = _provider("anthropic_messages", base_url="https://proxy.example.com/api/v3")
    with patch(
        "app.llm_adapters._post_json",
        return_value={"id": "msg_1", "content": [{"type": "text", "text": "ok"}]},
    ) as mock_post:
        chat_completion(provider, [{"role": "user", "content": "hi"}])

    assert mock_post.call_args.kwargs["url"] == "https://proxy.example.com/api/v3/messages"


def test_openai_responses_adapter_preserves_repeated_output_blocks():
    from app.llm_adapters import chat_completion

    provider = _provider("openai_responses")
    with patch(
        "app.llm_adapters._post_json",
        return_value={
            "id": "resp_1",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "repeat"},
                        {"type": "output_text", "text": "repeat"},
                    ],
                }
            ],
        },
    ):
        payload = chat_completion(provider, [{"role": "user", "content": "hi"}])

    assert payload["choices"][0]["message"]["content"] == "repeat\nrepeat"


def test_post_json_disables_transport_connect_retries():
    from app.llm_adapters import _post_json

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    provider = _provider("openai_chat", max_retries=2)
    with patch("app.llm_adapters.httpx.HTTPTransport") as mock_transport:
        with patch("app.llm_adapters.httpx.Client", return_value=FakeClient()):
            _post_json(provider=provider, url="https://provider.test", headers={}, body={})

    assert mock_transport.call_args.kwargs["retries"] == 0


def test_anthropic_adapter_converts_system_tools_and_tool_results():
    from app.llm_adapters import chat_completion

    provider = _provider("anthropic_messages")
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "create_reminder", "arguments": "{\"text\":\"开会\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"status\":\"ok\"}"},
    ]
    with patch(
        "app.llm_adapters._post_json",
        return_value={
            "id": "msg_1",
            "content": [
                {"type": "tool_use", "id": "call_2", "name": "create_reminder", "input": {"text": "复盘"}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        },
    ) as mock_post:
        payload = chat_completion(provider, messages, tools=_tool_schema())

    body = mock_post.call_args.kwargs["body"]
    assert body["system"] == "system prompt"
    assert body["tools"][0]["input_schema"]["required"] == ["text"]
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["id"] == "call_2"
    assert payload["usage"] == {"prompt_tokens": 12, "completion_tokens": 8}
