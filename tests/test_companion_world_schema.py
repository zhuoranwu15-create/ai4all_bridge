"""M2-A：朝夕相伴 P1 五表 schema + companion_world repo 原语 + L3 append-only/锚隔离。

覆盖：迁移建表/索引/唯一约束、universe 幂等 get-or-create、resident 偏唯一 + 容量真相、
ai_conversation 唯一、L3 append-only + fact_type 过滤 + status 过滤 + **跨 universe 锚隔离**，
以及 PG 并发 append 不覆盖（§9/D-12，SQLite skip）。
"""
import concurrent.futures

import pytest

import app.db as db
from app.db._backend import IntegrityError, is_postgres
from app.db._core import (
    _MIGRATIONS,
    _migration_0030_companion_world_candidates,
    _migration_0031_platform_user_quota_overrides,
    _migration_0032_rpm_hit_double_precision,
    _migration_0033_companion_world_m3_content,
    _migration_0034_companion_world_lifecycle_mailbox,
    _migration_0035_companion_world_visit_human_chat,
    _migration_0047_nooki_core,
)

_P1_TABLES = (
    "universes",
    "character_templates",
    "universe_residents",
    "ai_conversations",
    "universe_memory_facts",
)


def _pu(phone: str, name: str = "居民") -> str:
    return db.create_or_get_platform_user_by_phone(phone=phone, display_name=name)["id"]


def _account(pu: str, name: str = "居民") -> str:
    return db.create_ai4all_account_for_user(platform_user_id=pu, display_name=name)[
        "account"
    ]["id"]


def _runtime_account(name: str = "居民") -> str:
    """一个 form-B 居民 runtime 容器账号（bare account row，无 binding/wallet），供 create_resident 映射。

    决策 B：居民 runtime account 不是用户账号（不发 owner_binding、不占「一 App 一号」容量）。
    此 helper 直插一行 accounts 作运行容器——本文件的 resident 测试只需 FK 目标存在，无需 profile/钱包。
    """
    from app.db._core import _new_account_id

    acc = _new_account_id()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, channel, display_name, app_id, updated_at) "
            "VALUES (?, 'native', ?, 'zhaoxi', "
            "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))",
            (acc, name),
        )
    return acc


# ---------------------------------------------------------------------------
# 1. schema / 迁移
# ---------------------------------------------------------------------------
def test_p1_tables_exist(fresh_db):
    # 后端中立：对每张表跑一条空结果查询，缺表即抛错。
    for table in _P1_TABLES:
        with db.connect() as conn:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()


def test_m0030_schema_and_idempotency(fresh_db):
    """m0030 已登记、列可查询，且重复执行不会重复加列/索引。"""
    assert _MIGRATIONS[-1] == (
        47,
        _migration_0047_nooki_core,
    )
    with db.connect() as conn:
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        assert int(version) == 47
        _migration_0030_companion_world_candidates(conn)
        _migration_0030_companion_world_candidates(conn)
        _migration_0034_companion_world_lifecycle_mailbox(conn)
        _migration_0034_companion_world_lifecycle_mailbox(conn)
        _migration_0035_companion_world_visit_human_chat(conn)
        _migration_0035_companion_world_visit_human_chat(conn)
        conn.execute(
            "SELECT initial_candidate_rank FROM character_templates WHERE 1 = 0"
        ).fetchall()


def test_active_initial_candidate_rank_is_unique(fresh_db):
    """仅 active 模板的非空 initial rank 唯一；retired 历史版本可保留同 rank。"""
    active = db.create_character_template(source_type="official", name="首发-A")
    retired = db.create_character_template(
        source_type="official", name="历史-A", status="retired"
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE character_templates SET initial_candidate_rank = 1 WHERE id = ?",
            (active["id"],),
        )
        conn.execute(
            "UPDATE character_templates SET initial_candidate_rank = 1 WHERE id = ?",
            (retired["id"],),
        )
    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "UPDATE character_templates SET status = 'active' WHERE id = ?",
                (retired["id"],),
            )


def test_legacy_residents_can_share_sentinel_template(fresh_db):
    """同世界多个 legacy account 可共用哨兵模板，不受非 legacy 模板唯一索引影响。"""
    pu = _pu("19911110012")
    world = db.get_or_create_home_universe(platform_user_id=pu)
    sentinel = db.create_character_template(source_type="operations", name="legacy")
    residents = [
        db.create_resident(
            universe_id=world["id"],
            character_template_id=sentinel["id"],
            template_version="legacy",
            origin="legacy",
            status="active",
            runtime_account_id=_runtime_account(name),
        )
        for name in ("legacy-甲", "legacy-乙")
    ]
    assert len({resident["id"] for resident in residents}) == 2


def test_nonlegacy_template_is_unique_within_universe(fresh_db):
    """同一世界不能用同一模板建立两段非 legacy 关系。"""
    pu = _pu("19911110013")
    world = db.get_or_create_home_universe(platform_user_id=pu)
    template = db.create_character_template(source_type="official", name="首发-B")
    db.create_resident(
        universe_id=world["id"],
        character_template_id=template["id"],
        template_version="v1",
        origin="preset",
    )
    with pytest.raises(IntegrityError):
        db.create_resident(
            universe_id=world["id"],
            character_template_id=template["id"],
            template_version="v1",
            origin="custom",
        )


def test_universe_owner_unique(fresh_db):
    pu = _pu("19911110001")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    # 直插第二个同 owner 的 universe → 命中 UNIQUE(owner_platform_user_id)。
    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO universes(id, owner_platform_user_id, status, onboarding_state) "
                "VALUES (?, ?, 'active', 'preparing')",
                ("uni_dup", pu),
            )
    assert w["owner_platform_user_id"] == pu


def test_resident_runtime_partial_unique(fresh_db):
    """ux_universe_residents_runtime：非空 runtime_account_id 唯一；candidate 期 NULL 可多行。"""
    pu = _pu("19911110002")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    acc = _account(pu)
    templates = [
        db.create_character_template(source_type="official", name=f"小满-{index}")["id"]
        for index in range(4)
    ]
    db.create_resident(
        universe_id=w["id"],
        character_template_id=templates[0],
        template_version="v1",
        origin="preset",
        status="active",
        runtime_account_id=acc,
    )
    # 同一 runtime account 第二个 resident → 偏唯一索引挡下。
    with pytest.raises(IntegrityError):
        db.create_resident(
            universe_id=w["id"],
            character_template_id=templates[1],
            template_version="v1",
            origin="preset",
            status="active",
            runtime_account_id=acc,
        )
    # candidate 期 runtime_account_id 为 NULL → 偏索引不约束，可多行。
    db.create_resident(
        universe_id=w["id"], character_template_id=templates[2], template_version="v1", origin="preset"
    )
    db.create_resident(
        universe_id=w["id"], character_template_id=templates[3], template_version="v1", origin="preset"
    )


def test_ai_conversation_unique_resident(fresh_db):
    pu = _pu("19911110003")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    acc = _account(pu)
    tmpl = db.create_character_template(source_type="official", name="小满")["id"]
    res = db.create_resident(
        universe_id=w["id"],
        character_template_id=tmpl,
        template_version="v1",
        origin="preset",
        status="active",
        runtime_account_id=acc,
    )
    c1 = db.create_ai_conversation(
        universe_id=w["id"],
        resident_id=res["id"],
        owner_platform_user_id=pu,
        runtime_account_id=acc,
    )
    # 幂等：同 resident 再建命中 ux_ai_conversations_resident → 返回既有会话、不新建。
    c2 = db.create_ai_conversation(
        universe_id=w["id"],
        resident_id=res["id"],
        owner_platform_user_id=pu,
        runtime_account_id=acc,
    )
    assert c1["id"] == c2["id"]
    # owner-scoped 读：非本人 → None（§3.4-2 防枚举）。
    assert db.get_conversation(conversation_id=c1["id"], owner_platform_user_id=pu) is not None
    assert db.get_conversation(conversation_id=c1["id"], owner_platform_user_id="pu_other") is None


# ---------------------------------------------------------------------------
# 2. repo：universe 幂等 + 容量真相
# ---------------------------------------------------------------------------
def test_home_universe_idempotent(fresh_db):
    pu = _pu("19911110004")
    a = db.get_or_create_home_universe(platform_user_id=pu)
    b = db.get_or_create_home_universe(platform_user_id=pu)
    assert a["id"] == b["id"]  # 不叠加、命中同一行


def test_count_active_residents_only_active(fresh_db):
    pu = _pu("19911110005")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    templates = [
        db.create_character_template(source_type="official", name=f"小满-{index}")["id"]
        for index in range(3)
    ]
    # 两个居民 runtime 容器账号（form-B bare account，非用户账号）。#42 收敛后同真人只有一个用户
    # 账号，故不能用 create_ai4all 建第二个；居民容器直接建 account 行即可。
    acc1, acc2 = _runtime_account("甲"), _runtime_account("乙")
    db.create_resident(
        universe_id=w["id"], character_template_id=templates[0], template_version="v1",
        origin="preset", status="active", runtime_account_id=acc1,
    )
    db.create_resident(
        universe_id=w["id"], character_template_id=templates[1], template_version="v1",
        origin="preset", status="offline", runtime_account_id=acc2,
    )
    db.create_resident(
        universe_id=w["id"], character_template_id=templates[2], template_version="v1", origin="preset",
        status="candidate",
    )
    # 容量真相 = status='active' 计数（D-07）：offline/candidate 不计位。
    assert db.count_active_residents(universe_id=w["id"]) == 1
    assert len(db.list_residents(universe_id=w["id"], statuses=("active", "offline"))) == 2


# ---------------------------------------------------------------------------
# 3. L3 append-only + fact_type/status 过滤 + 跨 universe 锚隔离（核心）
# ---------------------------------------------------------------------------
def test_l3_append_only_and_anchor_isolation(fresh_db):
    pu_a, pu_b = _pu("19911110006", "甲"), _pu("19911110007", "乙")
    wa = db.get_or_create_home_universe(platform_user_id=pu_a)
    wb = db.get_or_create_home_universe(platform_user_id=pu_b)

    db.append_universe_fact(
        universe_id=wa["id"], fact_type="bazi", payload_json='{"gan":"甲"}',
        occurred_at="2026-07-20 10:00:00", source_account_id="accX",
    )
    db.append_universe_fact(
        universe_id=wa["id"], fact_type="user_identity", payload_json='{"name":"甲"}',
        occurred_at="2026-07-20 10:01:00", source_account_id="accY",
    )
    db.append_universe_fact(
        universe_id=wb["id"], fact_type="bazi", payload_json='{"other":true}',
        occurred_at="2026-07-20 10:02:00",
    )

    a_all = db.read_universe_facts(universe_id=wa["id"])
    assert len(a_all) == 2  # append-only：两行都在，无覆盖
    assert all(r["universe_id"] == wa["id"] for r in a_all)  # 锚隔离：绝不含 wb 行
    # fact_type 过滤
    assert [r["fact_type"] for r in db.read_universe_facts(universe_id=wa["id"], fact_types=["bazi"])] == ["bazi"]
    # 跨 universe 不可见
    b_all = db.read_universe_facts(universe_id=wb["id"])
    assert len(b_all) == 1 and b_all[0]["payload_json"] == '{"other":true}'
    # provenance 保留
    assert any(r["source_account_id"] == "accX" for r in a_all)


def test_l3_write_rejects_cross_universe_resident(fresh_db):
    """§2.5 写入隔离（codex finding ③）：source_resident_id 必属 target universe，跨 universe/不存在 → 拒写回滚。"""
    pu_a, pu_b = _pu("19911110010", "甲"), _pu("19911110011", "乙")
    wa = db.get_or_create_home_universe(platform_user_id=pu_a)
    wb = db.get_or_create_home_universe(platform_user_id=pu_b)
    tmpl = db.create_character_template(source_type="official", name="小满")["id"]
    res_b = db.create_resident(
        universe_id=wb["id"], character_template_id=tmpl, template_version="v1",
        origin="preset", status="active", runtime_account_id=_account(pu_b),
    )

    # 居民 B（属世界 B）往世界 A 写 → 拒（跨 universe 污染）。
    with pytest.raises(ValueError):
        db.append_universe_fact(
            universe_id=wa["id"], fact_type="user_fact", payload_json='{"x":1}',
            occurred_at="2026-07-21 10:00:00", source_resident_id=res_b["id"],
        )
    # 不存在的 resident → 拒。
    with pytest.raises(ValueError):
        db.append_universe_fact(
            universe_id=wa["id"], fact_type="user_fact", payload_json='{"x":1}',
            occurred_at="2026-07-21 10:00:00", source_resident_id="res_ghost",
        )
    # 拒写回滚：世界 A 无任何 fact 落库。
    assert db.read_universe_facts(universe_id=wa["id"]) == []

    # 正路：居民 B 往**自己**世界 B 写 → 成功。
    fid = db.append_universe_fact(
        universe_id=wb["id"], fact_type="user_fact", payload_json='{"x":1}',
        occurred_at="2026-07-21 10:00:00", source_resident_id=res_b["id"],
    )
    assert fid and len(db.read_universe_facts(universe_id=wb["id"])) == 1


def test_l3_status_filter_excludes_superseded(fresh_db):
    """compact 后被合并行 status='superseded' 不再进默认读（active）。"""
    pu = _pu("19911110008")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    fid = db.append_universe_fact(
        universe_id=w["id"], fact_type="user_preference", payload_json='{"food":"辣"}',
        occurred_at="2026-07-20 10:00:00",
    )
    # 模拟单 writer compact：把该行标记 superseded。
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_memory_facts SET status = 'superseded' WHERE id = ?", (fid,)
        )
    assert db.read_universe_facts(universe_id=w["id"], status="active") == []
    assert len(db.read_universe_facts(universe_id=w["id"], status="superseded")) == 1


# ---------------------------------------------------------------------------
# 4. PG 并发：同一 universe 两 resident 并发 append 不覆盖（§9/D-12，SQLite skip）
# ---------------------------------------------------------------------------
def test_concurrent_l3_append_no_overwrite(fresh_db):
    if not is_postgres():
        pytest.skip("L3 append-only 并发不覆盖只在 PG 算数（§9 硬门禁，SQLite 单写者不作数）")

    pu = _pu("19911110009")
    w = db.get_or_create_home_universe(platform_user_id=pu)
    per = 20  # 每个来源 append 20 条

    def _task(i: int):
        src = "acc-A" if i % 2 == 0 else "acc-B"
        return db.append_universe_fact(
            universe_id=w["id"], fact_type="user_event", payload_json=f'{{"i":{i}}}',
            occurred_at="2026-07-20 10:00:00", source_account_id=src,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(_task, range(2 * per)))

    rows = db.read_universe_facts(universe_id=w["id"], fact_types=["user_event"])
    # append-only 无锁：2*per 条各自落库，无 last-writer-wins 覆盖。
    assert len(rows) == 2 * per
    assert len(set(ids)) == 2 * per
    a = sum(1 for r in rows if r["source_account_id"] == "acc-A")
    b = sum(1 for r in rows if r["source_account_id"] == "acc-B")
    assert a == per and b == per
