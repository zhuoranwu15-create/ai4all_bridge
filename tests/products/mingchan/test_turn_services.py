"""鸣蝉居民 turn 的产品归属、Prompt 与工具隔离。"""
from __future__ import annotations

import json

import app.db as db
from app.agent_runtime.ports import MemoryEvent, MemoryProvenance
from app.bootstrap.product_registry import MINGCHAN_APP_ID, build_test_product_registry
from app.products.mingchan.application.memory import build_mingchan_world_memory_sink
from app.products.mingchan.application.companion_world_turn import (
    run_mingchan_companion_world_turn,
)
from app.products.mingchan.application.identity import create_mingchan_login_session
from app.products.mingchan.application.turn_services import MingchanTurnServices
from app.products.mingchan.domain.companion_world.onboarding import (
    MingchanWorldOnboardingService,
    ResidentSelection,
)
from app.products.mingchan.infrastructure.world_onboarding import (
    SqlMingchanWorldOnboardingRepository,
)


def _verified_token(phone: str) -> str:
    verification = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    return str(
        db.set_verification_verified(
            verification["id"],
            token_expires_minutes=10,
        )["verified_token"]
    )


def _resident_scope(phone: str) -> tuple[dict, object]:
    registry = build_test_product_registry()
    login = create_mingchan_login_session(
        phone=phone,
        verified_token=_verified_token(phone),
        registry=registry,
    )
    db.create_character_template(
        app_id=MINGCHAN_APP_ID,
        template_id="tmpl_mingchan_turn",
        source_type="operations",
        name="鸣蝉测试居民",
        avatar_ref="asset://mingchan-turn",
        summary="测试居民",
        tags_json=json.dumps(["稳定", "温柔", "测试"], ensure_ascii=False),
        persona_seed_json=json.dumps(
            {
                "SOUL.md": "# SOUL\n\n你是鸣蝉世界里的独立居民。",
                "IDENTITY.md": "# IDENTITY\n\n- 你的名字是鸣蝉测试居民",
            },
            ensure_ascii=False,
        ),
        persona_version="v1",
        initial_candidate_rank=1,
    )
    for rank in range(2, 5):
        db.create_character_template(
            app_id=MINGCHAN_APP_ID,
            template_id=f"tmpl_mingchan_turn_{rank}",
            source_type="operations",
            name=f"鸣蝉测试居民{rank}",
            avatar_ref=f"asset://mingchan-turn-{rank}",
            summary=f"测试居民{rank}",
            tags_json=json.dumps(["稳定", "温柔", f"测试{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n鸣蝉居民人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 名字：测试居民{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )
    service = MingchanWorldOnboardingService(
        SqlMingchanWorldOnboardingRepository(registry=registry)
    )
    user_id = str(login["registration"]["platform_user"]["id"])
    candidate = service.bootstrap_home(user_id).candidates[0]
    resident = service.confirm_residents(
        user_id,
        [ResidentSelection(template_id=candidate.template.id)],
    )[0]
    return {
        "platform_user_id": user_id,
        "universe_id": resident.universe_id,
        "resident_id": resident.resident_id,
        "runtime_account_id": resident.runtime_account_id,
        "conversation_id": resident.conversation_id,
    }, registry


def test_mingchan_resident_turn_uses_own_product_services_and_shared_tools(
    fresh_db, monkeypatch
):
    scope, registry = _resident_scope("13800037930")
    captured = {}
    original_prepare_session = MingchanTurnServices.prepare_session

    def capture_prepare_session(self, **kwargs):
        captured["memory_sink"] = kwargs["memory_sink"]
        return original_prepare_session(self, **kwargs)

    def fake_generate_reply_with_tools(**kwargs):
        captured["tools"] = {
            item["function"]["name"] for item in kwargs.get("tools") or []
        }
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["app_id"] = kwargs["ctx"].app_id
        return "鸣蝉回复", None

    monkeypatch.setattr("app.agent_runtime.turns.service.settings", fresh_db)
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.generate_reply_with_tools",
        fake_generate_reply_with_tools,
    )

    def capture_chat_charge(**kwargs):
        captured["billing_registry"] = kwargs["registry"]
        return None

    monkeypatch.setattr(
        "app.agent_runtime.turns.service.record_chat_usage_charge",
        capture_chat_charge,
    )
    monkeypatch.setattr(
        MingchanTurnServices,
        "prepare_session",
        capture_prepare_session,
    )

    result = run_mingchan_companion_world_turn(
        **scope,
        sender_name="鸣蝉用户",
        message_id="mingchan-turn-1",
        text="你好",
        registry=registry,
    )

    assert result.status == "ok"
    assert result.reply == "鸣蝉回复"
    assert captured["app_id"] == MINGCHAN_APP_ID
    assert captured["billing_registry"] is registry
    assert captured["memory_sink"] is not None
    assert "你是鸣蝉世界里的独立居民" in captured["system_prompt"]
    assert {"web_fetch", "read"} <= captured["tools"]
    assert "create_reminder" not in captured["tools"]
    assert "mission_status" not in captured["tools"]
    with db.connect() as conn:
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id = ?",
            (scope["runtime_account_id"],),
        ).fetchone()
        messages = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE account_id = ? AND message_id != ?
            ORDER BY id
            """,
            (
                scope["runtime_account_id"],
                f"welcome-{scope['resident_id']}",
            ),
        ).fetchall()
    assert account["app_id"] == MINGCHAN_APP_ID
    assert [(row["role"], row["content"]) for row in messages] == [
        ("user", "你好"),
        ("assistant", "鸣蝉回复"),
    ]


def test_mingchan_disabled_registry_rejects_before_turn_side_effects(
    fresh_db, monkeypatch
):
    scope, _registry = _resident_scope("13800037932")
    with db.connect() as conn:
        before = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE account_id = ?",
                (scope["runtime_account_id"],),
            ).fetchone()["n"]
        )

    def should_not_prepare(self, **kwargs):  # pragma: no cover - 守卫失败才会触发
        raise AssertionError("disabled product must fail before session preparation")

    monkeypatch.setattr(MingchanTurnServices, "prepare_session", should_not_prepare)
    result = run_mingchan_companion_world_turn(
        **scope,
        sender_name="鸣蝉用户",
        message_id="mingchan-disabled-turn",
        text="这条消息不能落库",
        registry=build_test_product_registry(mingchan_enabled=False),
    )

    assert result.status == "disabled"
    assert result.metadata["reason"] == "product_disabled"
    with db.connect() as conn:
        after = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE account_id = ?",
                (scope["runtime_account_id"],),
            ).fetchone()["n"]
        )
    assert after == before


def test_mingchan_memory_sink_writes_only_mingchan_resident_scope(fresh_db):
    scope, _registry = _resident_scope("13800037931")
    sink = build_mingchan_world_memory_sink()
    assert sink is not None
    event = MemoryEvent(
        fact_type="user_preference",
        payload={
            "memory_text": "用户喜欢安静的雨夜。",
            "operation": "add",
            "target_file": "MEMORY.md",
            "category": "preference",
        },
        provenance=MemoryProvenance(
            source_account_id=scope["runtime_account_id"],
            turn_message_id="mingchan-memory-1",
            session_id=1,
            business_day="2026-08-04",
            occurred_at="2026-08-04T12:00:00+08:00",
        ),
    )

    sink.emit(event)

    facts = db.read_universe_facts(universe_id=scope["universe_id"])
    assert len(facts) == 1
    assert "安静的雨夜" in facts[0]["payload_json"]
    assert facts[0]["source_account_id"] == scope["runtime_account_id"]
