"""M2-C C4：typed sink、universe 锚写入与确定性 compact。"""
import json
from unittest.mock import patch

import pytest

import app.db as db
from app.agent_runtime.ports import MemoryEvent, MemoryProvenance
from app.products.zhaoxi.domain.companion_world.memory_sink import CompanionWorldMemorySink
from app.products.zhaoxi.application import (
    build_companion_world_memory_sink,
    compact_companion_world_memory_batch,
)
from tests.factories import create_account, make_resident_account


def _event(
    account_id: str,
    *,
    fact_type: str = "user_preference",
    text: str = "用户喜欢清淡口味。",
    message_id: str = "message-001",
) -> MemoryEvent:
    return MemoryEvent(
        fact_type=fact_type,
        payload={
            "memory_text": text,
            "operation": "add",
            "target_file": "MEMORY.md",
            "category": "preference",
        },
        provenance=MemoryProvenance(
            source_account_id=account_id,
            turn_message_id=message_id,
            session_id=1,
            business_day="2026-07-21",
            occurred_at="2026-07-21T10:00:00+08:00",
        ),
    )


def test_domain_sink_routes_l3_keeps_l2_local_and_rejects_unknown():
    emitted = []
    sink = CompanionWorldMemorySink(emitted.append)
    for fact_type in (
        "user_identity",
        "user_preference",
        "user_profile_derived",
        "user_event",
    ):
        sink.emit(_event("acc", fact_type=fact_type))
    sink.emit(_event("acc", fact_type="relationship"))
    sink.emit(_event("acc", fact_type="commitment"))

    assert [event.fact_type for event in emitted] == [
        "user_identity",
        "user_preference",
        "user_profile_derived",
        "user_event",
    ]
    with pytest.raises(ValueError, match="unsupported_memory_fact_type"):
        sink.emit(_event("acc", fact_type="raw_conversation"))


def test_platform_sink_is_noop_for_form_a_and_isolates_universes(fresh_db):
    form_a = "acc-form-a-memory"
    create_account(form_a)
    sink = build_companion_world_memory_sink()
    sink.emit(_event(form_a))
    assert db.resolve_resident_memory_scope(runtime_account_id=form_a) is None

    user_a = db.create_or_get_platform_user_by_phone(
        phone="19950003001", display_name="A"
    )
    user_b = db.create_or_get_platform_user_by_phone(
        phone="19950003002", display_name="B"
    )
    account_a = make_resident_account(user_a["id"], "居民A")
    account_b = make_resident_account(user_b["id"], "居民B")
    scope_a = db.resolve_resident_memory_scope(runtime_account_id=account_a)
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)

    sink.emit(_event(account_a, text="A 世界偏好", message_id="message-a"))
    sink.emit(_event(account_b, text="B 世界偏好", message_id="message-b"))
    facts_a = db.read_universe_facts(universe_id=scope_a["universe_id"])
    facts_b = db.read_universe_facts(universe_id=scope_b["universe_id"])

    assert len(facts_a) == len(facts_b) == 1
    assert "A 世界偏好" in facts_a[0]["payload_json"]
    assert "B 世界偏好" not in facts_a[0]["payload_json"]
    assert facts_a[0]["source_resident_id"] == scope_a["resident_id"]
    assert facts_b[0]["source_resident_id"] == scope_b["resident_id"]


def test_l3_background_switch_disables_sink_and_compact(fresh_db, monkeypatch):
    monkeypatch.setattr(
        "app.products.zhaoxi.application.companion_world_memory.settings.companion_world_l3_background_enabled",
        False,
    )
    assert build_companion_world_memory_sink() is None
    assert compact_companion_world_memory_batch(limit=10) == {
        "scanned": 0,
        "merged_groups": 0,
        "superseded_facts": 0,
        "next_cursor": None,
        "results": [],
        "disabled": True,
    }
def test_dreaming_emits_only_applied_distilled_memory(fresh_db):
    from app.agent_runtime.persistence import profile_storage
    from app.dreaming import read_long_term_memory, run_dreaming

    fresh_db.llm_api_key = "fake-key"
    account_id = "acc-dream-typed-sink"
    profile_storage.write_file(
        account_id,
        "memory/2026-07-21.md",
        "# Daily\n\n用户原话：我喜欢少盐。",
    )
    payload = json.dumps(
        {
            "session_summary": {
                "carryover_summary": "用户偏好清淡口味。",
                "open_threads": [],
                "tone_notes": "",
            },
            "long_term_memory_items": [
                {
                    "fact_type": "user_preference",
                    "operation": "add",
                    "target_file": "MEMORY.md",
                    "category": "preference",
                    "memory_text": "用户喜欢清淡口味。",
                    "importance": "high",
                    "confidence": 0.95,
                    "sensitivity": "normal",
                    "reason": "稳定偏好",
                    "source_message_ids": ["client-public-001"],
                    "source_daily_note_dates": ["2026-07-21"],
                }
            ],
            "excluded_sensitive_items": [],
        },
        ensure_ascii=False,
    )
    events = []
    sink = CompanionWorldMemorySink(events.append)
    with patch(
        "app.agent_runtime.llm.service.generate_completion_with_usage", return_value=(payload, None)
    ):
        result = run_dreaming(
            account_id=account_id,
            today="2026-07-21",
            days=1,
            memory_sink=sink,
        )

    assert result["applied_count"] == 1
    assert "清淡口味" in read_long_term_memory(account_id)
    assert len(events) == 1
    assert events[0].fact_type == "user_preference"
    assert events[0].payload == {
        "memory_text": "用户喜欢清淡口味。",
        "operation": "add",
        "target_file": "MEMORY.md",
        "category": "preference",
    }
    assert events[0].provenance.turn_message_id == "client-public-001"
    assert "用户原话" not in json.dumps(events[0].payload, ensure_ascii=False)


def test_compact_exact_normalized_duplicates_is_idempotent(fresh_db):
    user = db.create_or_get_platform_user_by_phone(
        phone="19950003003", display_name="compact"
    )
    account_a = make_resident_account(user["id"], "居民一")
    account_b = make_resident_account(user["id"], "居民二")
    scope_a = db.resolve_resident_memory_scope(runtime_account_id=account_a)
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)
    universe_id = scope_a["universe_id"]
    assert scope_b["universe_id"] == universe_id

    first_id = db.append_universe_fact(
        universe_id=universe_id,
        fact_type="user_preference",
        payload_json='{"memory_text":"  用户 喜欢   茶  ","operation":"add"}',
        source_account_id=account_a,
        source_resident_id=scope_a["resident_id"],
        source_message_id="old-message",
        occurred_at="2026-07-21T09:00:00+08:00",
    )
    second_id = db.append_universe_fact(
        universe_id=universe_id,
        fact_type="user_preference",
        payload_json='{"operation":"add", "memory_text":"用户 喜欢 茶"}',
        source_account_id=account_b,
        source_resident_id=scope_b["resident_id"],
        source_message_id="new-message",
        occurred_at="2026-07-21T10:00:00+08:00",
    )
    db.append_universe_fact(
        universe_id=universe_id,
        fact_type="user_preference",
        payload_json='{"memory_text":"用户喜欢咖啡","operation":"add"}',
        occurred_at="2026-07-21T11:00:00+08:00",
    )

    compacted = db.compact_universe_facts(universe_id=universe_id)
    active = db.read_universe_facts(universe_id=universe_id)
    superseded = db.read_universe_facts(
        universe_id=universe_id, status="superseded"
    )

    assert compacted["merged_groups"] == 1
    assert compacted["superseded_facts"] == 2
    assert len(active) == 2
    merged = next(item for item in active if item["id"] in compacted["merged_fact_ids"])
    assert merged["source_account_id"] == account_b
    assert merged["source_message_id"] == "new-message"
    assert {item["id"] for item in superseded} == {first_id, second_id}
    assert {item["superseded_by"] for item in superseded} == {merged["id"]}
    assert db.compact_universe_facts(universe_id=universe_id)["merged_groups"] == 0

    batch = compact_companion_world_memory_batch(limit=10)
    assert batch["scanned"] == 1
    assert batch["merged_groups"] == 0
