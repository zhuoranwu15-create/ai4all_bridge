import time
from types import SimpleNamespace
from unittest.mock import patch

from app.agent_runtime.llm.providers import LLMProviderConfig
from app.platform.channels import get_channel_capability
from app.schemas import OpenClawTurnResponse


def _ctx():
    from app.agent_runtime.turns.service import ChannelTurnInput

    identity = SimpleNamespace(
        channel="app",
        session_key="session-key",
        sender_id="sender-1",
        chat_id="chat-1",
        channel_account_id="channel-account-1",
    )
    return ChannelTurnInput(
        account_id="account-1",
        app_id="plum",
        cap=get_channel_capability("app"),
        identity=identity,
        message_id="client-1",
        event_id="client-1",
        message_type="text",
        text="hello",
        media=None,
        raw={},
        sender_name=None,
        turn_id="turn-1",
        client_message_id="client-1",
        idempotency_key="idem-1",
        usage_billing_enabled=False,
    )


def _services():
    registry = SimpleNamespace(require_enabled=lambda app_id: None)
    return SimpleNamespace(app_id="plum", allowed_channels=("app",), registry=registry)


def _setup():
    from app.agent_runtime.turns.service import _TurnSetup

    return _TurnSetup(
        account_id="account-1",
        identity=_ctx().identity,
        account={},
        session={"id": 1},
        binding={},
        profile={},
        sender_id="sender-1",
        message_id="client-1",
        openclaw_session_key="session-key",
        today="2026-08-09",
        business_day="2026-08-09",
        now=None,
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        effective_daily=100,
        llm_provider=LLMProviderConfig(
            id="provider-1",
            label="Provider",
            protocol="openai_chat",
            base_url="https://provider.test",
            model="model-1",
            api_key="key",
        ),
        cap=get_channel_capability("app"),
    )


def _inbound():
    from app.agent_runtime.turns.service import _InboundResult

    return _InboundResult(
        text="hello",
        inserted_id=1,
        image_described=False,
        image_understanding_failed=False,
        inbound_screen=None,
        inbound_blocked=False,
        quota_reservation_id="quota-1",
    )


def _reply(text="你好。"):
    from app.agent_runtime.turns.service import _ReplyResult

    return _ReplyResult(
        reply=text,
        generation_error=None,
        normal_reply_generated=True,
        tool_names=[],
        onboarding_pre_extracted=None,
        system_prompt="system",
        llm_messages=[],
        debug_metadata={},
        llm_provider=_setup().llm_provider,
    )


def _patch_runtime(resolve):
    run = {"id": "turn-1", "status": "accepted"}
    return patch.multiple(
        "app.agent_runtime.turns.service",
        get_account_product_access=lambda account_id: None,
        _prepare_turn=lambda *args, **kwargs: _setup(),
        _persist_and_screen_inbound=lambda *args, **kwargs: _inbound(),
        create_runtime_turn_run=lambda **kwargs: (run, True),
        mark_runtime_turn_running=lambda turn_id: True,
        mark_runtime_turn_first_delta=lambda turn_id: True,
        finish_runtime_turn_run=lambda **kwargs: True,
        _resolve_turn_reply=resolve,
        _finalize_turn=lambda *args, **kwargs: OpenClawTurnResponse(
            status="ok", reply=kwargs.get("result", None), metadata={"reply_message_id": "reply-1"}
        ),
    )


def test_runtime_stream_reuses_phases_and_emits_visible_text_only():
    from app.agent_runtime.turns.service import run_product_turn_stream

    def resolve(*args, **kwargs):
        kwargs["on_text_delta"]("你好。")
        return _reply()

    with _patch_runtime(resolve):
        events = list(run_product_turn_stream(_ctx(), product_services=_services()))

    assert [event.kind for event in events] == ["accepted", "text_delta", "completed"]
    assert events[1].text == "你好。"
    assert events[1].seq == 1


def test_runtime_stream_cancellation_persists_partial_terminal():
    from app.agent_runtime.llm.service import LLMStreamCancelled
    from app.agent_runtime.turns.service import CancellationToken, run_product_turn_stream

    def resolve(*args, **kwargs):
        kwargs["on_text_delta"]("部分。")
        while not kwargs["is_cancelled"]():
            time.sleep(0.001)
        raise LLMStreamCancelled("cancelled")

    cancellation = CancellationToken()
    with _patch_runtime(resolve):
        iterator = run_product_turn_stream(
            _ctx(),
            product_services=_services(),
            cancellation=cancellation,
        )
        assert next(iterator).kind == "accepted"
        assert next(iterator).kind == "text_delta"
        cancellation.cancel()
        assert next(iterator).kind == "cancelled"
