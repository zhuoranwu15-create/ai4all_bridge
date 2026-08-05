"""S3 命名与候选身份：名池校验、确定性选名、快照读回与候选 DTO 字段（NAME-001 / CAND-001）。

分三层：领域层纯函数（无 DB）、service 层快照语义（同世界重复 bootstrap / 未配名池退化 /
跨真人隔离）、API 层候选 DTO 新字段与安全字段仍不外泄。运营导入侧的名池校验与原地更新
在 ``test_companion_world_presets.py`` 之外单列，因为它们属于 S3 而非首发目录导入。
"""
import copy
import json

import pytest

import app.db as db
from app.bootstrap.product_registry import build_test_product_registry
from app.products.mingchan.application import SqlCompanionWorldRepository
from app.products.mingchan.domain.companion_world import (
    CompanionWorldService,
    ResidentSelection,
)
from app.products.mingchan.domain.companion_world.naming import (
    NAMING_STATUS_READY,
    NAMING_STATUS_UNAVAILABLE,
    NamePoolError,
    naming_status,
    normalize_name_pool,
    select_suggested_name,
)
from scripts.import_companion_world_presets import import_presets, validate_manifest

_POOL = ("小满", "阿棠", "青禾", "林间")


@pytest.fixture
def client(mingchan_client):
    """使用已启用鸣蝉注册表的隔离测试客户端。"""

    return mingchan_client


def _user(phone: str) -> str:
    platform_user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name="用户"
    )["id"]
    db.ensure_product_membership(
        platform_user_id=platform_user_id,
        app_id="mingchan",
        registry=build_test_product_registry(),
    )
    return platform_user_id


def _seed(rank: int) -> str:
    return json.dumps(
        {
            "SOUL.md": f"# SOUL\n\n人格 {rank}",
            "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是 角色{rank}",
        },
        ensure_ascii=False,
    )


def _seed_catalog(*, with_pool: bool = True) -> list[dict]:
    """四条首发模板；``with_pool=False`` 模拟运营尚未配名池的线上现状。"""
    return [
        db.create_character_template(
            template_id=f"tmpl_naming_{rank}",
            source_type="operations",
            name=f"角色{rank}",
            avatar_ref=f"asset://avatar-{rank}",
            summary=f"简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=_seed(rank),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
            persona_key=f"persona_{rank}",
            long_summary=f"长介绍{rank}",
            name_pool_json=(
                json.dumps(list(_POOL), ensure_ascii=False) if with_pool else None
            ),
            name_pool_version="np_v1" if with_pool else None,
        )
        for rank in range(1, 5)
    ]


def _service() -> CompanionWorldService:
    return CompanionWorldService(
        SqlCompanionWorldRepository(registry=build_test_product_registry())
    )


# ---------------------------------------------------------------------------
# 1. 领域层纯函数
# ---------------------------------------------------------------------------
def test_normalize_name_pool_accepts_three_to_five_and_strips():
    assert normalize_name_pool([" 甲 ", "乙", "丙"]) == ("甲", "乙", "丙")
    assert len(normalize_name_pool(["甲", "乙", "丙", "丁", "戊"])) == 5


@pytest.mark.parametrize(
    "names",
    [
        ["甲", "乙"],  # 少于 3 个：失去「同模板不同用户不同实例名」的意义
        ["甲", "乙", "丙", "丁", "戊", "己"],  # 多于 5 个
        ["甲", "甲", "乙"],  # 重名
        ["甲", "乙", " "],  # 空白名
        ["甲", "乙", "ab"],  # 控制字符 → 展示名白名单挡下
    ],
)
def test_normalize_name_pool_rejects_invalid(names):
    with pytest.raises(NamePoolError):
        normalize_name_pool(names)


def test_select_suggested_name_is_deterministic_and_pool_scoped():
    kwargs = dict(universe_id="uni_a", template_id="tmpl_1", name_pool=_POOL)
    first = select_suggested_name(**kwargs, name_pool_version="np_v1")
    assert first == select_suggested_name(**kwargs, name_pool_version="np_v1")
    assert first in _POOL


def test_select_suggested_name_varies_by_universe_template_and_version():
    """三个输入都参与选名：换世界/换模板/换名池版本都可能换名（不要求必然不同，但不得恒等）。"""
    base = dict(name_pool=_POOL, name_pool_version="np_v1")
    by_universe = {
        select_suggested_name(universe_id=f"uni_{i}", template_id="tmpl_1", **base)
        for i in range(24)
    }
    by_template = {
        select_suggested_name(universe_id="uni_a", template_id=f"tmpl_{i}", **base)
        for i in range(24)
    }
    by_version = {
        select_suggested_name(
            universe_id="uni_a",
            template_id="tmpl_1",
            name_pool=_POOL,
            name_pool_version=f"np_v{i}",
        )
        for i in range(24)
    }
    assert by_universe == by_template == by_version == set(_POOL)


def test_select_suggested_name_is_stable_across_processes():
    """选名不得依赖内建 hash()（按进程加盐）：子进程必须算出同一个名字。"""
    import subprocess
    import sys

    snippet = (
        "from app.products.mingchan.domain.companion_world.naming import select_suggested_name;"
        "print(select_suggested_name(universe_id='uni_a', template_id='tmpl_1',"
        f" name_pool={list(_POOL)!r}, name_pool_version='np_v1'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    assert out == select_suggested_name(
        universe_id="uni_a",
        template_id="tmpl_1",
        name_pool=_POOL,
        name_pool_version="np_v1",
    )


def test_naming_status_only_depends_on_snapshot():
    assert naming_status("小满") == NAMING_STATUS_READY
    assert naming_status(None) == NAMING_STATUS_UNAVAILABLE
    assert naming_status("   ") == NAMING_STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# 2. service：快照写一次、之后只读回
# ---------------------------------------------------------------------------
def test_bootstrap_snapshots_name_once_and_survives_pool_change(fresh_db):
    user_id = _user("19940001001")
    _seed_catalog()
    first = _service().bootstrap_home(user_id)
    names = [item.suggested_display_name for item in first.candidates]
    assert all(name in _POOL for name in names)
    assert [item.naming_version for item in first.candidates] == ["np_v1"] * 4

    # 运营换名池 + 换版本；已快照的候选必须原样返回（老用户的名字不会一夜之间变掉）。
    with db.connect() as conn:
        conn.execute(
            "UPDATE character_templates SET name_pool_json=?, name_pool_version='np_v2'",
            (json.dumps(["甲", "乙", "丙"], ensure_ascii=False),),
        )
    second = _service().bootstrap_home(user_id)
    assert [item.suggested_display_name for item in second.candidates] == names
    assert [item.naming_version for item in second.candidates] == ["np_v1"] * 4


def test_bootstrap_without_name_pool_still_succeeds_as_unavailable(fresh_db):
    """模板未配名池 → 候选无实例名，但 bootstrap 必须成功（NAME-001 验收口径）。"""
    user_id = _user("19940001002")
    _seed_catalog(with_pool=False)
    result = _service().bootstrap_home(user_id)
    assert len(result.candidates) == 4
    assert all(item.suggested_display_name is None for item in result.candidates)
    # 没名字就不该留版本号，避免「有版本却没名字」的自相矛盾行。
    assert all(item.naming_version is None for item in result.candidates)


def test_suggested_name_is_isolated_per_owner_world(fresh_db):
    """选名以 universe_id 为输入：两个真人各自快照，互不影响、互不可见。"""
    _seed_catalog()
    a = _service().bootstrap_home(_user("19940001003"))
    b = _service().bootstrap_home(_user("19940001004"))
    assert a.world.id != b.world.id
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT universe_id, suggested_display_name FROM universe_residents"
        ).fetchall()
    assert {row["universe_id"] for row in rows} == {a.world.id, b.world.id}
    assert all(row["suggested_display_name"] in _POOL for row in rows)


def test_confirm_defaults_to_snapshot_name_then_template_name(fresh_db):
    """客户端不传称呼时用快照实例名；未配名池才退回模板工作名。"""
    user_id = _user("19940001005")
    _seed_catalog()
    service = _service()
    boot = service.bootstrap_home(user_id)
    residents = service.confirm_residents(
        user_id, [ResidentSelection(template_id=boot.candidates[0].template.id)]
    )
    assert residents[0].name == boot.candidates[0].suggested_display_name

    fallback_user = _user("19940001006")
    with db.connect() as conn:
        conn.execute(
            "UPDATE character_templates SET name_pool_json=NULL, name_pool_version=NULL"
        )
    fallback_boot = service.bootstrap_home(fallback_user)
    fallback = service.confirm_residents(
        fallback_user,
        [ResidentSelection(template_id=fallback_boot.candidates[0].template.id)],
    )
    assert fallback[0].name == "角色1"


# ---------------------------------------------------------------------------
# 3. API：候选 DTO 新字段
# ---------------------------------------------------------------------------
def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    return db.set_verification_verified(row["id"], token_expires_minutes=10)[
        "verified_token"
    ]


def _login(client, phone: str) -> dict:
    response = client.post(
        "/api/v1/products/mingchan/auth/session",
        json={"phone": phone, "verified_token": _verified_token(phone)},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_candidate_dto_exposes_naming_fields_without_persona(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    headers = _login(client, "19940001007")
    first = client.post("/api/v1/products/mingchan/worlds/home/bootstrap", headers=headers)
    assert first.status_code == 200, first.text
    candidate = first.json()["data"]["candidates"][0]
    assert candidate["suggested_display_name"] in _POOL
    assert candidate["naming_status"] == NAMING_STATUS_READY
    assert candidate["naming_version"] == "np_v1"
    assert candidate["persona_key"] == "persona_1"
    assert candidate["long_summary"] == "长介绍1"
    # CAND-001 只放开身份字段；人设正文与内部 id 仍不出网。
    assert "persona_seed_json" not in first.text and "resident_id" not in first.text

    # 换设备/重装 = 重新 bootstrap + 重新拉候选，两处返回必须与首次完全一致。
    again = client.get("/api/v1/products/mingchan/worlds/home/resident-candidates", headers=headers)
    assert again.json()["data"]["candidates"][0] == candidate


def test_candidate_dto_reports_unavailable_when_pool_missing(client, fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog(with_pool=False)
    headers = _login(client, "19940001008")
    response = client.post("/api/v1/products/mingchan/worlds/home/bootstrap", headers=headers)
    assert response.status_code == 200, response.text
    for candidate in response.json()["data"]["candidates"]:
        assert candidate["suggested_display_name"] is None
        assert candidate["naming_status"] == NAMING_STATUS_UNAVAILABLE


# ---------------------------------------------------------------------------
# 4. 运营导入：名池落库、原地更新与不可变边界
# ---------------------------------------------------------------------------
def _manifest(*, with_pool: bool = True) -> list[dict]:
    items = []
    for rank in range(1, 5):
        item = {
            "template_id": f"tmpl_ops_v1_{rank}",
            "initial_candidate_rank": rank,
            "name": f"运营角色{rank}",
            "avatar_ref": f"asset://v1/{rank}",
            "summary": f"简介 {rank}",
            "tags": ["温柔", "好奇", f"类型{rank}"],
            "persona_seed_json": {
                "SOUL.md": f"# SOUL\n\nv1-{rank}",
                "IDENTITY.md": f"# IDENTITY\n\nv1-{rank}",
            },
            "persona_version": "v1",
            "persona_key": f"persona_{rank}",
        }
        if with_pool:
            item["name_pool"] = list(_POOL[:3])
            item["name_pool_version"] = "np_v1"
            item["long_summary"] = f"长介绍{rank}"
        items.append(item)
    return items


def test_manifest_requires_pool_and_version_together():
    partial = _manifest()
    del partial[0]["name_pool_version"]
    with pytest.raises(ValueError, match="must be set together"):
        validate_manifest(partial)


def test_manifest_rejects_invalid_name_pool():
    bad = _manifest()
    bad[0]["name_pool"] = ["甲", "甲", "乙"]
    with pytest.raises(ValueError, match="duplicates"):
        validate_manifest(bad)


def test_import_writes_pool_and_updates_published_template_in_place(fresh_db):
    """名池是运营元数据：已发布模板可原地补配/改版，无需换 template_id。"""
    import_presets(validate_manifest(_manifest(with_pool=False)), dry_run=False)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT name_pool_json FROM character_templates WHERE id='tmpl_ops_v1_1'"
        ).fetchone()["name_pool_json"] is None

    report = import_presets(validate_manifest(_manifest()), dry_run=False)
    assert report.errors == [] and report.create_ids == []
    assert sorted(report.update_ids) == [f"tmpl_ops_v1_{rank}" for rank in range(1, 5)]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name_pool_json, name_pool_version, long_summary "
            "FROM character_templates WHERE id='tmpl_ops_v1_1'"
        ).fetchone()
    assert json.loads(row["name_pool_json"]) == list(_POOL[:3])
    assert row["name_pool_version"] == "np_v1"
    assert row["long_summary"] == "长介绍1"

    # 重放同一份 manifest 不再产生更新（幂等）。
    assert import_presets(validate_manifest(_manifest()), dry_run=False).update_ids == []


def test_replaying_manifest_without_pool_does_not_wipe_configured_pool(fresh_db):
    import_presets(validate_manifest(_manifest()), dry_run=False)
    report = import_presets(validate_manifest(_manifest(with_pool=False)), dry_run=False)
    assert report.errors == [] and report.update_ids == []
    with db.connect() as conn:
        row = conn.execute(
            "SELECT name_pool_json, name_pool_version FROM character_templates "
            "WHERE id='tmpl_ops_v1_1'"
        ).fetchone()
    assert json.loads(row["name_pool_json"]) == list(_POOL[:3])
    assert row["name_pool_version"] == "np_v1"


def test_persona_key_is_immutable_once_assigned(fresh_db):
    import_presets(validate_manifest(_manifest()), dry_run=False)
    changed = copy.deepcopy(_manifest())
    changed[0]["persona_key"] = "persona_other"
    report = import_presets(validate_manifest(changed), dry_run=False)
    assert report.errors and "persona_key is immutable" in report.errors[0]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT persona_key FROM character_templates WHERE id='tmpl_ops_v1_1'"
        ).fetchone()["persona_key"] == "persona_1"
