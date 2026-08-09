import json
from unittest.mock import patch

from app.agent_runtime.llm.providers import LLMProviderConfig


def _provider(protocol: str) -> LLMProviderConfig:
    return LLMProviderConfig(
        id="stream-test",
        label="Stream Test",
        protocol=protocol,
        base_url="https://provider.test",
        model="model-1",
        api_key="secret",
        force_ipv4=False,
        max_retries=0,
    )


def test_sse_parser_handles_arbitrary_utf8_and_record_boundaries():
    from app.agent_runtime.llm.adapters import _iter_sse_records

    wire = (
        'event: message.delta\r\ndata: {"text":"你"}\r\n\r\n'
        'event: completed\ndata: {}\n\n'
    ).encode("utf-8")
    chunks = [wire[:37], wire[37:38], wire[38:41], wire[41:55], wire[55:]]

    assert list(_iter_sse_records(chunks)) == [
        ("message.delta", '{"text":"你"}'),
        ("completed", "{}"),
    ]


def test_openai_chat_stream_normalizes_text_tools_usage_and_completion():
    from app.agent_runtime.llm.adapters import _openai_chat_stream_events

    records = [
        (
            "message",
            {
                "id": "chat_1",
                "choices": [
                    {
                        "delta": {
                            "content": "你好",
                            "reasoning_content": "must stay private",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "lookup", "arguments": '{"q"'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ),
        (
            "message",
            {
                "id": "chat_1",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ':"北京"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        ),
        ("message", None),
    ]

    events = list(_openai_chat_stream_events(records))

    assert [event.kind for event in events] == [
        "text_delta",
        "tool_delta",
        "usage",
        "tool_delta",
        "completed",
    ]
    assert events[0].text == "你好"
    assert "private" not in "".join(event.text for event in events)
    assert "".join(event.tool_call["arguments_delta"] for event in events if event.tool_call) == '{"q":"北京"}'
    assert events[-1].finish_reason == "tool_calls"


def test_openai_responses_stream_ignores_reasoning_and_normalizes_tool_delta():
    from app.agent_runtime.llm.adapters import _openai_responses_stream_events

    records = [
        ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
        (
            "response.reasoning_text.delta",
            {"type": "response.reasoning_text.delta", "delta": "must stay private"},
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "call_id": "call_1", "name": "lookup"},
            },
        ),
        (
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "call_1",
                "delta": '{"q":"上海"}',
            },
        ),
        (
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "完成"},
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "usage": {"input_tokens": 5, "output_tokens": 6}},
            },
        ),
    ]

    events = list(_openai_responses_stream_events(records))

    assert [event.kind for event in events] == [
        "tool_delta",
        "tool_delta",
        "text_delta",
        "usage",
        "completed",
    ]
    assert "".join(event.text for event in events) == "完成"
    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].response_id == "resp_1"


def test_anthropic_stream_normalizes_text_tools_usage_and_completion():
    from app.agent_runtime.llm.adapters import _anthropic_stream_events

    records = [
        (
            "message_start",
            {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 7}}},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":"广州"}'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "must stay private"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "text_delta", "text": "收到"},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]

    events = list(_anthropic_stream_events(records))

    assert [event.kind for event in events] == [
        "tool_delta",
        "tool_delta",
        "text_delta",
        "usage",
        "completed",
    ]
    assert "".join(event.text for event in events) == "收到"
    assert events[-2].usage == {"prompt_tokens": 7, "completion_tokens": 8}
    assert events[-1].finish_reason == "tool_calls"


def test_openai_responses_truncated_stream_becomes_safe_error_event():
    from app.agent_runtime.llm.adapters import _openai_responses_stream_events

    events = list(
        _openai_responses_stream_events(
            [("response.output_text.delta", {"type": "response.output_text.delta", "delta": "partial"})]
        )
    )

    assert events[-1].kind == "error"
    assert events[-1].error_code == "stream_truncated"


def test_openai_chat_requires_done_or_finish_reason():
    from app.agent_runtime.llm.adapters import _openai_chat_stream_events

    truncated = list(
        _openai_chat_stream_events(
            [
                (
                    "message",
                    {
                        "id": "chat_partial",
                        "choices": [
                            {"delta": {"content": "partial"}, "finish_reason": None}
                        ],
                    },
                )
            ]
        )
    )
    compatible_clean_eof = list(
        _openai_chat_stream_events(
            [
                (
                    "message",
                    {
                        "id": "chat_complete",
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                    },
                )
            ]
        )
    )

    assert truncated[-1].kind == "error"
    assert truncated[-1].error_code == "stream_truncated"
    assert compatible_clean_eof[-1].kind == "completed"
    assert compatible_clean_eof[-1].finish_reason == "stop"


def test_stream_payload_honors_preexisting_cancellation_before_provider_call():
    from app.agent_runtime.llm.service import (
        LLMStreamCancelled,
        _http_chat_stream_payload,
    )

    with patch("app.agent_runtime.llm.service.chat_completion_stream") as mock_stream:
        try:
            _http_chat_stream_payload(
                [{"role": "user", "content": "hi"}],
                provider=_provider("openai_chat"),
                is_cancelled=lambda: True,
            )
        except LLMStreamCancelled:
            pass
        else:
            raise AssertionError("expected preexisting cancellation")

    mock_stream.assert_not_called()


def test_chat_completion_stream_sets_protocol_stream_fields():
    from app.agent_runtime.llm.adapters import chat_completion_stream

    cases = [
        ("openai_chat", "stream_options"),
        ("openai_responses", None),
        ("anthropic_messages", None),
    ]
    for protocol, extra_field in cases:
        with patch(
            "app.agent_runtime.llm.adapters._stream_sse_json",
            return_value=iter([("message", None)]) if protocol == "openai_chat" else iter([]),
        ) as mock_stream:
            list(chat_completion_stream(_provider(protocol), [{"role": "user", "content": "hi"}]))
        body = mock_stream.call_args.kwargs["body"]
        assert body["stream"] is True
        if extra_field:
            assert body[extra_field] == {"include_usage": True}


def test_closing_stream_iterator_exits_http_contexts():
    from app.agent_runtime.llm.adapters import _stream_sse_json

    state = {"client_exited": False, "response_exited": False}

    class FakeResponse:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b'data: {"type":"one"}\n\n'
            yield b'data: {"type":"two"}\n\n'

    class ResponseContext:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            state["response_exited"] = True
            return False

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            state["client_exited"] = True
            return False

        def stream(self, *args, **kwargs):
            return ResponseContext()

    with patch("app.agent_runtime.llm.adapters.httpx.Client", return_value=FakeClient()):
        iterator = _stream_sse_json(
            provider=_provider("openai_chat"),
            url="https://provider.test/chat/completions",
            headers={},
            body={"stream": True},
        )
        assert next(iterator)[1] == {"type": "one"}
        iterator.close()

    assert state == {"client_exited": True, "response_exited": True}
