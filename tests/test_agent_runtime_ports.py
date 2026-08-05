"""M2-A：agent_runtime 端口骨架的形状 smoke（纯类型，无行为、无 DB）。

只断言 ADR §7.3 四接缝 DTO/Protocol 存在且字段/方法齐——防止后续刀误删/改签名而无感知。
"""
import dataclasses
from types import SimpleNamespace

from app.agent_runtime import ports


def test_dtos_exist_with_fields():
    prov_fields = {f.name for f in dataclasses.fields(ports.MemoryProvenance)}
    assert {"source_account_id", "turn_message_id", "session_id", "business_day", "occurred_at"} <= prov_fields

    ev_fields = {f.name for f in dataclasses.fields(ports.MemoryEvent)}
    assert {"fact_type", "payload", "provenance"} <= ev_fields

    intent_fields = {f.name for f in dataclasses.fields(ports.ProactiveIntent)}
    assert {
        "platform_user_id", "speaker_account_id", "category", "text",
        "idempotency_key", "universe_id", "resident_id", "product_category", "metadata",
    } <= intent_fields

    dr_fields = {f.name for f in dataclasses.fields(ports.DeliveryResult)}
    assert {"status", "channel", "ref_id"} <= dr_fields

    rh_fields = {f.name for f in dataclasses.fields(ports.ResidentHandle)}
    assert {"resident_id", "runtime_account_id", "universe_id"} <= rh_fields


def test_dtos_are_frozen():
    p = ports.MemoryProvenance(
        source_account_id="a", turn_message_id=None, session_id=None,
        business_day="2026-07-20", occurred_at="2026-07-20T10:00:00+08:00",
    )
    ev = ports.MemoryEvent(fact_type="bazi", payload={"x": 1}, provenance=p)
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.fact_type = "user_identity"  # type: ignore[misc]


def test_proactive_intent_form_a_defaults():
    # 形态 A（微信）：universe_id/resident_id 缺省为 None，metadata 缺省空 dict。
    intent = ports.ProactiveIntent(
        platform_user_id="pu", speaker_account_id="acc", category="USER_REMINDER",
        text="在吗", idempotency_key="k",
    )
    assert intent.universe_id is None and intent.resident_id is None
    assert intent.metadata == {}


def test_protocols_declare_methods():
    # 四接缝 Protocol 存在，且声明了本刀落定的方法。
    assert hasattr(ports.MemorySink, "emit")
    assert hasattr(ports.ProactiveDeliveryAdapter, "deliver")
    assert hasattr(ports.AgentRuntimePort, "send_turn")
    assert hasattr(ports.UnitOfWork, "__enter__") and hasattr(ports.UnitOfWork, "__exit__")


def test_default_adapter_delegates_turn(monkeypatch):
    from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter

    expected = object()
    services = object()
    monkeypatch.setattr(
        "app.agent_runtime.adapter.run_product_turn",
        lambda ctx, *, product_services: (ctx, product_services, expected),
    )
    marker = object()
    assert DefaultAgentRuntimeAdapter(services).send_turn(marker) == (
        marker,
        services,
        expected,
    )


def test_runtime_requires_explicit_matching_product_services(monkeypatch):
    """通用 Runtime 不得为缺失/错配的产品服务隐式回落朝夕。"""

    import pytest

    from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter
    from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn
    from app.platform.auth.identity import ResolvedIdentity
    from app.platform.channels import CHANNELS, CHANNEL_APP

    with pytest.raises(TypeError):
        DefaultAgentRuntimeAdapter()

    identity = ResolvedIdentity(
        ai4all_account_id="acc-test",
        session_key="app:test",
        channel=CHANNEL_APP,
        channel_account_id="pu-test",
        sender_id="pu-test",
        chat_id=None,
    )
    ctx = ChannelTurnInput(
        account_id="acc-test",
        app_id="test_product",
        cap=CHANNELS[CHANNEL_APP],
        identity=identity,
        message_id="msg-test",
        event_id=None,
        message_type="text",
        text="hello",
        media=None,
        raw={},
        sender_name=None,
    )
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.get_account_product_access",
        lambda **_: pytest.fail("产品服务错配必须在数据库访问前拒绝"),
    )

    result = run_product_turn(
        ctx,
        product_services=SimpleNamespace(app_id="zhaoxi"),
    )

    assert result.status == "disabled"
    assert result.metadata["reason"] == "product_service_mismatch"


def test_runtime_rejects_product_channel_mismatch_before_database(monkeypatch):
    """产品不允许的渠道必须在账号读取和消息副作用前失败。"""

    import pytest

    from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn
    from app.platform.auth.identity import ResolvedIdentity
    from app.platform.channels import CHANNELS, CHANNEL_WEIXIN

    identity = ResolvedIdentity(
        ai4all_account_id="acc-mingchan-weixin",
        session_key="weixin:test",
        channel=CHANNEL_WEIXIN,
        channel_account_id="bot-test",
        sender_id="user-test",
        chat_id="chat-test",
    )
    ctx = ChannelTurnInput(
        account_id="acc-mingchan-weixin",
        app_id="mingchan",
        cap=CHANNELS[CHANNEL_WEIXIN],
        identity=identity,
        message_id="msg-test",
        event_id=None,
        message_type="text",
        text="hello",
        media=None,
        raw={},
        sender_name=None,
    )
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.get_account_product_access",
        lambda **_: pytest.fail("渠道错配必须在数据库访问前拒绝"),
    )

    result = run_product_turn(
        ctx,
        product_services=SimpleNamespace(
            app_id="mingchan",
            allowed_channels=("native",),
        ),
    )

    assert result.status == "disabled"
    assert result.metadata["reason"] == "product_channel_mismatch"
