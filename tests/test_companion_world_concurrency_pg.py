"""Companion World 的 PG 权威并发门禁（SQLite 单写者不作数）。"""
import concurrent.futures
import json
import threading

import pytest

import app.db as db
from app.db._backend import is_postgres
from app.domains.companion_world import (
    CompanionWorldError,
    CompanionWorldService,
    ResidentSelection,
    TemplateDraft,
)
from app.platform import SqlCompanionWorldRepository
from tests.factories import make_resident_account


def _draft(name: str) -> TemplateDraft:
    return TemplateDraft(
        name=name,
        persona_seed_json=json.dumps(
            {"SOUL.md": f"# SOUL\n\n{name}", "IDENTITY.md": f"# IDENTITY\n\n{name}"},
            ensure_ascii=False,
        ),
    )


def test_concurrent_tenth_and_eleventh_resident_only_one_commits(fresh_db):
    if not is_postgres():
        pytest.skip("world row lock 并发正确性以 PG 为准")

    user_id = db.create_or_get_platform_user_by_phone(
        phone="19950002001", display_name="并发用户"
    )["id"]
    for rank in range(1, 5):
        db.create_character_template(
            source_type="operations",
            name=f"首发{rank}",
            avatar_ref=f"asset://{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"soul-{rank}", "IDENTITY.md": f"identity-{rank}"}
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )
    service = CompanionWorldService(SqlCompanionWorldRepository())
    boot = service.bootstrap_home(user_id)
    service.confirm_residents(
        user_id,
        [ResidentSelection(item.template.id) for item in boot.candidates],
    )
    for index in range(5, 10):
        service.create_resident(user_id, custom_template=_draft(f"居民{index}"))
    assert len(service.list_residents(user_id)) == 9

    barrier = threading.Barrier(2)

    def _create(index: int) -> str:
        local = CompanionWorldService(SqlCompanionWorldRepository())
        barrier.wait(timeout=5)
        try:
            local.create_resident(
                user_id, custom_template=_draft(f"并发居民{index}")
            )
            return "created"
        except CompanionWorldError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_create, (10, 11)))

    assert sorted(results) == ["created", "resident_capacity_exceeded"]
    assert len(service.list_residents(user_id)) == 10
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 10
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE name LIKE '并发居民%'"
        ).fetchone()["c"] == 1


def test_two_residents_concurrently_append_l3_without_overwrite(fresh_db):
    if not is_postgres():
        pytest.skip("L3 多 writer append 并发正确性以 PG 为准")

    user_id = db.create_or_get_platform_user_by_phone(
        phone="19950002002", display_name="L3 并发用户"
    )["id"]
    account_a = make_resident_account(user_id, "L3居民A")
    account_b = make_resident_account(user_id, "L3居民B")
    scope_a = db.resolve_resident_memory_scope(runtime_account_id=account_a)
    scope_b = db.resolve_resident_memory_scope(runtime_account_id=account_b)
    assert scope_a["universe_id"] == scope_b["universe_id"]
    universe_id = scope_a["universe_id"]
    barrier = threading.Barrier(2)

    def _append(account_id: str, resident_id: str, marker: str) -> str:
        barrier.wait(timeout=5)
        return db.append_universe_fact(
            universe_id=universe_id,
            fact_type="user_event",
            payload_json=json.dumps({"marker": marker}),
            source_account_id=account_id,
            source_resident_id=resident_id,
            source_message_id=f"message-{marker}",
            occurred_at="2026-07-22T10:00:00+08:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                _append, account_a, scope_a["resident_id"], "a"
            ),
            executor.submit(
                _append, account_b, scope_b["resident_id"], "b"
            ),
        )
        fact_ids = {future.result(timeout=10) for future in futures}

    facts = db.read_universe_facts(universe_id=universe_id)
    assert {item["id"] for item in facts} == fact_ids
    assert {json.loads(item["payload_json"])["marker"] for item in facts} == {
        "a",
        "b",
    }
