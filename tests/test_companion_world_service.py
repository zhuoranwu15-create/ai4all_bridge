"""M2-C C1：领域 service + SQL repository + 原子居民实例化。"""
import json

import pytest

import app.db as db
from app.products.zhaoxi.domain.companion_world import (
    CompanionWorldError,
    CompanionWorldService,
    ResidentSelection,
    TemplateDraft,
)
from app.products.zhaoxi.application import SqlCompanionWorldRepository


def _user(phone: str) -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone, display_name="用户")["id"]


def _seed(rank: int) -> str:
    return json.dumps(
        {
            "SOUL.md": f"# SOUL\n\n首发人格 {rank}",
            "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是 角色{rank}",
        },
        ensure_ascii=False,
    )


def _seed_catalog() -> list[dict]:
    return [
        db.create_character_template(
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://avatar-{rank}",
            summary=f"角色 {rank} 简介",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=_seed(rank),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )
        for rank in range(1, 5)
    ]


def _service() -> CompanionWorldService:
    return CompanionWorldService(SqlCompanionWorldRepository())


def test_bootstrap_snapshots_four_candidates_and_is_idempotent(fresh_db):
    user_id = _user("19920001001")
    templates = _seed_catalog()

    first = _service().bootstrap_home(user_id)
    assert first.world.onboarding_state == "selecting"
    assert [item.template.initial_candidate_rank for item in first.candidates] == [1, 2, 3, 4]
    assert [item.template_version for item in first.candidates] == ["v1", "v2", "v3", "v4"]

    # 已快照的旧版随后 retired，重放 bootstrap 仍返回原四条，不换目录、不改版本。
    with db.connect() as conn:
        conn.execute(
            "UPDATE character_templates SET status='retired' WHERE id=?",
            (templates[0]["id"],),
        )
    second = _service().bootstrap_home(user_id)
    assert second.world.id == first.world.id
    assert [item.resident_id for item in second.candidates] == [
        item.resident_id for item in first.candidates
    ]
    assert second.candidates[0].template_version == "v1"


def test_bootstrap_rejects_incomplete_catalog_without_partial_candidates(fresh_db):
    user_id = _user("19920001002")
    _seed_catalog()[:3]
    # 删除 rank=4 模板，模拟上线目录不完整。
    with db.connect() as conn:
        conn.execute("DELETE FROM character_templates WHERE initial_candidate_rank=4")

    with pytest.raises(CompanionWorldError) as raised:
        _service().bootstrap_home(user_id)
    assert raised.value.code == "preset_catalog_not_ready"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM universe_residents").fetchone()["c"] == 0
        # world 与候选同事务：目录闸失败连 preparing world 也不留半行。
        assert conn.execute("SELECT COUNT(*) c FROM universes").fetchone()["c"] == 0


def test_confirm_is_atomic_idempotent_and_has_no_form_a_side_effects(fresh_db):
    user_id = _user("19920001003")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    chosen = [
        ResidentSelection(template_id=boot.candidates[0].template.id),
        ResidentSelection(template_id=boot.candidates[1].template.id, display_name="自定义称呼"),
    ]

    first = service.confirm_residents(user_id, chosen)
    second = service.confirm_residents(user_id, chosen)
    assert len(first) == len(second) == 2
    assert [item.resident_id for item in second] == [item.resident_id for item in first]
    assert [item.runtime_account_id for item in second] == [
        item.runtime_account_id for item in first
    ]
    assert {item.name for item in first} == {"角色1", "自定义称呼"}

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM ai_conversations").fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM account_profile_files WHERE filename IN ('SOUL.md','IDENTITY.md')"
        ).fetchone()["c"] == 4
        assert conn.execute("SELECT COUNT(*) c FROM account_owner_bindings").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM entitlement_wallets").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"] == 0
        states = {
            row["status"]: row["c"]
            for row in conn.execute(
                "SELECT status, COUNT(*) c FROM universe_residents GROUP BY status"
            ).fetchall()
        }
        assert states == {"active": 2, "dismissed": 2}
        assert conn.execute(
            "SELECT onboarding_state FROM universes WHERE id=?", (boot.world.id,)
        ).fetchone()["onboarding_state"] == "confirmed"
        for resident in first:
            assert db.resolve_owner_platform_user_id(
                conn, resident.runtime_account_id
            ) == user_id


def test_conversation_failure_rolls_back_account_profile_and_activation(
    fresh_db, monkeypatch
):
    user_id = _user("19920001004")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    selected = boot.candidates[0]

    def _fail_conversation(**_kwargs):
        raise RuntimeError("forced conversation failure")

    monkeypatch.setattr(
        "app.products.zhaoxi.infrastructure.repositories.companion_world.world_db.create_ai_conversation",
        _fail_conversation,
    )
    with pytest.raises(RuntimeError, match="forced conversation failure"):
        service.confirm_residents(
            user_id, [ResidentSelection(template_id=selected.template.id)]
        )

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM account_profile_files").fetchone()["c"] == 0
        row = conn.execute(
            "SELECT status, runtime_account_id FROM universe_residents WHERE id=?",
            (selected.resident_id,),
        ).fetchone()
        assert row["status"] == "candidate" and row["runtime_account_id"] is None


def test_owner_scoped_resident_and_conversation_resolution(fresh_db):
    _seed_catalog()
    service = _service()
    user_a = _user("19920001005")
    user_b = _user("19920001006")
    boot_a = service.bootstrap_home(user_a)
    boot_b = service.bootstrap_home(user_b)
    resident_a = service.confirm_residents(
        user_a, [ResidentSelection(boot_a.candidates[0].template.id)]
    )[0]
    service.confirm_residents(
        user_b, [ResidentSelection(boot_b.candidates[1].template.id)]
    )

    assert service.resolve_conversation(
        user_a, resident_a.conversation_id
    ).runtime_account_id == resident_a.runtime_account_id
    with pytest.raises(CompanionWorldError) as raised:
        service.resolve_conversation(user_b, resident_a.conversation_id)
    assert raised.value.code == "conversation_not_found"
    assert {item.resident_id for item in service.list_residents(user_a)} == {
        resident_a.resident_id
    }
def test_selecting_allows_only_one_custom_candidate(fresh_db):
    user_id = _user("19920001007")
    _seed_catalog()
    service = _service()
    service.bootstrap_home(user_id)
    draft = TemplateDraft(
        name="自建角色",
        persona_seed_json=json.dumps(
            {"SOUL.md": "# SOUL\n\n自建", "IDENTITY.md": "# IDENTITY\n\n自建"},
            ensure_ascii=False,
        ),
    )
    candidate = service.create_resident(user_id, custom_template=draft)
    assert candidate.origin == "custom" and candidate.status == "candidate"
    with pytest.raises(CompanionWorldError) as raised:
        service.create_resident(user_id, custom_template=draft)
    assert raised.value.code == "custom_candidate_limit_exceeded"


def test_confirmed_world_enforces_capacity_without_leaving_orphans(fresh_db):
    user_id = _user("19920001009")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    service.confirm_residents(
        user_id, [ResidentSelection(boot.candidates[0].template.id)]
    )

    for index in range(2, 11):
        service.create_resident(
            user_id,
            custom_template=TemplateDraft(
                name=f"自建角色{index}",
                persona_seed_json=json.dumps(
                    {
                        "SOUL.md": f"# SOUL\n\n自建{index}",
                        "IDENTITY.md": f"# IDENTITY\n\n自建{index}",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    with pytest.raises(CompanionWorldError) as raised:
        service.create_resident(
            user_id,
            custom_template=TemplateDraft(
                name="第十一位",
                persona_seed_json=json.dumps(
                    {"SOUL.md": "soul-11", "IDENTITY.md": "identity-11"}
                ),
            ),
        )
    assert raised.value.code == "resident_capacity_exceeded"
    assert len(service.list_residents(user_id)) == 10
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 10
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE name='第十一位'"
        ).fetchone()["c"] == 0


def test_repository_legacy_primitives_preserve_existing_runtime(fresh_db):
    user_id = _user("19920001008")
    account_id = db.create_ai4all_account_for_user(
        platform_user_id=user_id, display_name="存量角色"
    )["account"]["id"]
    repository = SqlCompanionWorldRepository()
    with repository.transaction() as tx:
        world = tx.get_or_create_home_universe(user_id)
        world = tx.lock_universe(world.id)
        assert tuple(tx.list_active_legacy_account_ids(user_id)) == (account_id,)
        resident = tx.ensure_legacy_resident(world, account_id)
        world = tx.mark_legacy_world(world.id, account_id)

    assert resident.runtime_account_id == account_id
    assert resident.origin == "legacy"
    assert world.legacy_primary_account_id == account_id
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# D-A（2026-07-26）：微信老用户在 App 侧按新用户走选择角色页，并带入既有角色。
# ---------------------------------------------------------------------------
def test_legacy_user_bootstrap_carries_in_wechat_resident_and_still_selects(fresh_db):
    _seed_catalog()
    user_id = _user("19920002001")
    account_id = db.create_ai4all_account_for_user(
        platform_user_id=user_id, display_name="微信上的小满"
    )["account"]["id"]

    result = _service().bootstrap_home(user_id)

    # 老用户不再被直接推进 confirmed：照样进选择页、照样拿四位候选。
    assert result.world.onboarding_state == "selecting"
    assert len(result.candidates) == 4
    assert all(item.origin == "preset" for item in result.candidates)
    # 微信角色作为已 active 的居民带入，且不出现在候选里（不可被叉掉）。
    assert len(result.residents) == 1
    carried = result.residents[0]
    assert carried.origin == "legacy"
    assert carried.status == "active"
    assert carried.runtime_account_id == account_id
    assert carried.name == "微信上的小满"
    # legacy primary 锚必须落库：微信侧主动消息路由依赖它。
    assert result.world.legacy_primary_account_id == account_id
    # 不新建 runtime account，复用微信侧既有账号。
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1


def test_legacy_carry_in_falls_back_to_default_display_name(fresh_db):
    """微信侧没起过名的账号，在 App 里显示产品默认名而不是哨兵串。"""
    _seed_catalog()
    user_id = _user("19920002002")
    account_id = db.create_ai4all_account_for_user(
        platform_user_id=user_id, display_name="占位"
    )["account"]["id"]
    # 模拟从未在微信 onboarding 里给 AI 起过名字的老账号。
    with db.connect() as conn:
        conn.execute(
            "UPDATE profiles SET display_name = NULL WHERE account_id = ?", (account_id,)
        )
        conn.commit()

    result = _service().bootstrap_home(user_id)

    assert [item.name for item in result.residents] == ["来自微信的Bot"]


def test_legacy_user_bootstrap_is_idempotent(fresh_db):
    _seed_catalog()
    user_id = _user("19920002003")
    db.create_ai4all_account_for_user(platform_user_id=user_id, display_name="存量角色")

    first = _service().bootstrap_home(user_id)
    second = _service().bootstrap_home(user_id)

    assert [item.resident_id for item in first.residents] == [
        item.resident_id for item in second.residents
    ]
    assert len(second.candidates) == 4
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM universe_residents WHERE origin='legacy'"
        ).fetchone()["c"] == 1


def test_new_user_bootstrap_has_no_carried_resident(fresh_db):
    _seed_catalog()
    user_id = _user("19920002004")

    result = _service().bootstrap_home(user_id)

    assert result.world.onboarding_state == "selecting"
    assert result.world.legacy_primary_account_id is None
    assert result.residents == ()
    assert len(result.candidates) == 4


def test_legacy_user_can_confirm_after_dismissing_every_preset(fresh_db):
    """Q2：带入的微信角色占名额、算「至少保留一位」，所以可以零选择确认。"""
    _seed_catalog()
    user_id = _user("19920002005")
    db.create_ai4all_account_for_user(platform_user_id=user_id, display_name="存量角色")
    service = _service()
    service.bootstrap_home(user_id)

    residents = service.confirm_residents(user_id, [])

    assert [item.origin for item in residents] == ["legacy"]
    world = SqlCompanionWorldRepository().get_home_universe(user_id)
    assert world.onboarding_state == "confirmed"


def test_new_user_cannot_confirm_with_empty_selection(fresh_db):
    """纯新用户世界里没人，零选择仍然必须被拒。"""
    _seed_catalog()
    user_id = _user("19920002006")
    service = _service()
    service.bootstrap_home(user_id)

    with pytest.raises(CompanionWorldError) as raised:
        service.confirm_residents(user_id, [])
    assert raised.value.code == "resident_capacity_empty"
