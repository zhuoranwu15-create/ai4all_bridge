"""M2-A：agent_runtime 端口骨架的形状 smoke（纯类型，无行为、无 DB）。

只断言 ADR §7.3 四接缝 DTO/Protocol 存在且字段/方法齐——防止后续刀误删/改签名而无感知。
"""
import dataclasses

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
    assert hasattr(ports.AgentRuntimePort, "resolve_conversation_account")
    assert hasattr(ports.UnitOfWork, "__enter__") and hasattr(ports.UnitOfWork, "__exit__")
