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
    _migration_0047_legacy_template_display_name,
    _migration_0048_companion_world_resident_drafts,
    _migration_0049_companion_world_naming,
    _migration_0050_ai_conversation_read_cursor,
    _migration_0051_app_me_tab,
    _migration_0052_human_conversation_read_cursor,
    _migration_0053_resident_intro_post,
    _migration_0054_media_assets,
    _migration_0055_universe_post_media,
    _migration_0056_media_moderation_scan_index,
    _migration_0057_resident_wish_drafts,
    _migration_0058_async_resident_wishes,
    _migration_0059_creator_role_templates,
    _migration_0060_creator_role_template_opening_and_summary,
    _migration_0061_companion_world_product_scope,
    _migration_0062_mingchan_notification_product_scope,
    _migration_0064_fibre_mvp,
    _migration_0065_fibre_character_experience,
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
        65,
        _migration_0065_fibre_character_experience,
    )
    with db.connect() as conn:
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        assert int(version) == 65
        _migration_0030_companion_world_candidates(conn)
        _migration_0030_companion_world_candidates(conn)
        _migration_0034_companion_world_lifecycle_mailbox(conn)
        _migration_0034_companion_world_lifecycle_mailbox(conn)
        _migration_0035_companion_world_visit_human_chat(conn)
        _migration_0035_companion_world_visit_human_chat(conn)
        conn.execute(
            "SELECT initial_candidate_rank FROM character_templates WHERE 1 = 0"
        ).fetchall()


def test_same_owner_can_keep_zhaoxi_and_create_mingchan_world(fresh_db):
    """同一真人的朝夕 legacy World 与鸣蝉新 World 必须并存且读取隔离。"""

    owner = _pu("19976000063", "双产品用户")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO universes(id, owner_platform_user_id, app_id) "
            "VALUES ('world_zhaoxi_63', ?, 'zhaoxi')",
            (owner,),
        )

    mingchan = db.get_or_create_home_universe(platform_user_id=owner)
    replay = db.get_or_create_home_universe(platform_user_id=owner)

    assert mingchan["app_id"] == "mingchan"
    assert replay["id"] == mingchan["id"]
    assert mingchan["id"] != "world_zhaoxi_63"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, app_id FROM universes WHERE owner_platform_user_id=? "
            "ORDER BY app_id",
            (owner,),
        ).fetchall()
    assert [(row["id"], row["app_id"]) for row in rows] == [
        (mingchan["id"], "mingchan"),
        ("world_zhaoxi_63", "zhaoxi"),
    ]


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


def test_initial_candidate_rank_is_isolated_by_product(fresh_db):
    """朝夕历史目录与鸣蝉目录可复用相同 rank，但产品内仍保持唯一。"""
    # 鸣蝉 persistence 有意拒绝跨产品写入；直接插入一行模拟迁移后的朝夕历史目录。
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO character_templates(
                id, app_id, source_type, name, initial_candidate_rank
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("tmpl_zhaoxi_history", "zhaoxi", "operations", "朝夕历史模板", 1),
        )
    mingchan = db.create_character_template(
        app_id="mingchan",
        source_type="operations",
        name="鸣蝉模板",
        initial_candidate_rank=1,
    )

    assert [
        row["id"]
        for row in db.list_initial_character_templates(app_id="mingchan")
    ] == [mingchan["id"]]
    with pytest.raises(IntegrityError):
        db.create_character_template(
            app_id="mingchan",
            source_type="operations",
            name="鸣蝉重复模板",
            initial_candidate_rank=1,
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
    # 同一产品内第二个 owner World 仍命中组合唯一约束。
    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO universes(id, owner_platform_user_id, app_id, status, onboarding_state) "
                "VALUES (?, ?, 'mingchan', 'active', 'preparing')",
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


def test_m0048_resident_drafts_schema_and_idempotency(fresh_db):
    """m0048：草稿表可用、结构化列已加，且重复执行不重复建表/加列。"""
    with db.connect() as conn:
        conn.execute("SELECT 1 FROM resident_drafts WHERE 1 = 0").fetchall()
        conn.execute(
            "SELECT persona_key, long_summary, relationship_type, personality_traits_json "
            "FROM character_templates WHERE 1 = 0"
        ).fetchall()
        _migration_0048_companion_world_resident_drafts(conn)
        _migration_0048_companion_world_resident_drafts(conn)
        conn.execute("SELECT 1 FROM resident_drafts WHERE 1 = 0").fetchall()


def test_resident_draft_token_is_unique_and_owner_scoped(fresh_db):
    """draft_token 全局唯一；同一真人的 client_request_id 唯一以承载幂等。"""
    pu = _pu("19911110048")
    other = _pu("19911110049")
    db.insert_resident_draft(
        platform_user_id=pu,
        draft_token="tok-a",
        name="草稿甲",
        avatar_key="linxiaoman",
        relationship_type="friend",
        relationship_label=None,
        personality_traits_json='["steady"]',
        style_note=None,
        normalized_summary="摘要",
        persona_seed_json='{"SOUL.md": "x", "IDENTITY.md": "y"}',
        safety_json=None,
        expires_at="2099-01-01 00:00:00",
    )
    with pytest.raises(IntegrityError):
        db.insert_resident_draft(
            platform_user_id=other,
            draft_token="tok-a",
            name="草稿乙",
            avatar_key="linxiaoman",
            relationship_type="friend",
            relationship_label=None,
            personality_traits_json='["steady"]',
            style_note=None,
            normalized_summary="摘要",
            persona_seed_json='{"SOUL.md": "x", "IDENTITY.md": "y"}',
            safety_json=None,
            expires_at="2099-01-01 00:00:00",
        )
    # 跨真人读取一律 not found：草稿按 owner 隔离。
    assert db.get_resident_draft_by_token(draft_token="tok-a", platform_user_id=other) is None
    assert db.get_resident_draft_by_token(draft_token="tok-a", platform_user_id=pu) is not None


def test_m0049_naming_columns_and_idempotency(fresh_db):
    """m0049：名池与候选选名列已加，且重复执行不重复加列。"""
    with db.connect() as conn:
        conn.execute(
            "SELECT name_pool_json, name_pool_version FROM character_templates WHERE 1 = 0"
        ).fetchall()
        conn.execute(
            "SELECT suggested_display_name, naming_version FROM universe_residents WHERE 1 = 0"
        ).fetchall()
        _migration_0049_companion_world_naming(conn)
        _migration_0049_companion_world_naming(conn)
        conn.execute(
            "SELECT name_pool_json, name_pool_version FROM character_templates WHERE 1 = 0"
        ).fetchall()


def test_m0050_read_cursor_column_and_idempotency(fresh_db):
    """m0050：read cursor 列已加、既有会话默认 NULL（= 一条都没读过），且重复执行不重复加列。"""
    with db.connect() as conn:
        conn.execute(
            "SELECT last_read_message_id FROM ai_conversations WHERE 1 = 0"
        ).fetchall()
        _migration_0050_ai_conversation_read_cursor(conn)
        _migration_0050_ai_conversation_read_cursor(conn)
        conn.execute(
            "SELECT last_read_message_id FROM ai_conversations WHERE 1 = 0"
        ).fetchall()


def test_m0051_me_tab_schema_and_idempotency(fresh_db):
    """m0051：Profile 列与两张「我的」表可查询，且重复执行不重复加列/建表。"""
    with db.connect() as conn:
        for statement in (
            "SELECT avatar_key FROM platform_users WHERE 1 = 0",
            "SELECT id, platform_user_id, app_id, status, reason_code, executed_at,"
            " purge_stats_json FROM account_deletion_requests WHERE 1 = 0",
            "SELECT platform_user_id, quiet_level FROM app_notification_preferences"
            " WHERE 1 = 0",
        ):
            conn.execute(statement).fetchall()
        _migration_0051_app_me_tab(conn)
        _migration_0051_app_me_tab(conn)
        for statement in (
            "SELECT avatar_key FROM platform_users WHERE 1 = 0",
            "SELECT id FROM account_deletion_requests WHERE 1 = 0",
            "SELECT platform_user_id FROM app_notification_preferences WHERE 1 = 0",
        ):
            conn.execute(statement).fetchall()


def test_m0052_human_read_cursor_schema_and_idempotency(fresh_db):
    """m0052：真人会话读游标列可查询，重复执行不重复加列，且默认 NULL（不回填）。"""
    with db.connect() as conn:
        conn.execute(
            "SELECT owner_last_read_sequence, visitor_last_read_sequence "
            "FROM human_conversations WHERE 1 = 0"
        ).fetchall()
        _migration_0052_human_conversation_read_cursor(conn)
        _migration_0052_human_conversation_read_cursor(conn)
        conn.execute(
            "SELECT owner_last_read_sequence, visitor_last_read_sequence "
            "FROM human_conversations WHERE 1 = 0"
        ).fetchall()


def test_m0053_resident_intro_index_and_idempotency(fresh_db):
    """m0053：自我介绍动态的偏唯一索引可用，重复执行迁移不报错。"""
    with db.connect() as conn:
        _migration_0053_resident_intro_post(conn)
        _migration_0053_resident_intro_post(conn)

    pu = _pu("19911110053")
    universe = db.get_or_create_home_universe(platform_user_id=pu)
    template = db.create_character_template(
        source_type="operations", name="人设", persona_version="v1"
    )
    resident = db.create_resident(
        universe_id=universe["id"],
        character_template_id=template["id"],
        template_version="v1",
        origin="preset",
        status="active",
        runtime_account_id=_runtime_account(),
    )
    with db.connect() as conn:
        rows = [
            db.publish_resident_intro_post_with_outbox(
                universe_id=universe["id"],
                author_resident_id=resident["id"],
                text=f"介绍 {index}",
                published_at="2026-07-29 10:00:00",
                conn=conn,
            )
            for index in range(2)
        ]
    # 同一居民只允许一条 resident_intro：第二次落到既有行、created=False。
    assert rows[0][1] is True and rows[1][1] is False
    assert rows[0][0]["id"] == rows[1][0]["id"]
    assert rows[1][0]["text"] == "介绍 0"


def test_m0054_media_assets_and_idempotency(fresh_db):
    """m0054：媒体表与三个消息侧列可查询，重复执行迁移不报错。"""
    with db.connect() as conn:
        _migration_0054_media_assets(conn)
        _migration_0054_media_assets(conn)
        # 后端中立：空结果查询覆盖全部列名，缺列即抛错。
        conn.execute(
            "SELECT id, owner_platform_user_id, kind, mime, bytes, width, height, "
            "duration_ms, sha256, storage_path, transcript, status, moderation_status, "
            "moderation_task_id, expires_at, created_at FROM media_assets WHERE 1 = 0"
        ).fetchall()
        conn.execute(
            "SELECT content_json, media_id FROM messages WHERE 1 = 0"
        ).fetchall()
        conn.execute("SELECT media_id FROM human_messages WHERE 1 = 0").fetchall()


def test_m0055_universe_post_media_and_idempotency(fresh_db):
    """m0055：图文动态关联表可查询，重复执行迁移不报错。"""
    with db.connect() as conn:
        _migration_0055_universe_post_media(conn)
        _migration_0055_universe_post_media(conn)
        conn.execute(
            "SELECT post_id, media_id, position, created_at "
            "FROM universe_post_media WHERE 1 = 0"
        ).fetchall()


def test_m0056_media_moderation_scan_index_and_idempotency(fresh_db):
    """m0056：待审资产扫描路径与重试计数列可查询，重复执行迁移不报错。"""
    with db.connect() as conn:
        _migration_0056_media_moderation_scan_index(conn)
        _migration_0056_media_moderation_scan_index(conn)
        conn.execute(
            "SELECT id, moderation_attempts FROM media_assets "
            "WHERE moderation_status = 'pending' ORDER BY created_at ASC"
        ).fetchall()


def test_m0057_wish_draft_columns_and_idempotency(fresh_db):
    """m0057：许愿来源与许愿幂等键可查询，重复执行迁移不报错。"""
    with db.connect() as conn:
        _migration_0057_resident_wish_drafts(conn)
        _migration_0057_resident_wish_drafts(conn)
        conn.execute(
            "SELECT id, source, wish_request_id FROM resident_drafts "
            "WHERE platform_user_id = 'nobody' AND source = 'wish' "
            "ORDER BY created_at ASC"
        ).fetchall()


def test_m0058_async_wish_tables_and_mailbox_link_are_idempotent(fresh_db):
    """m0058：独立 wish/job 与可选 mailbox 来源字段两后端均可重复执行。"""
    with db.connect() as conn:
        _migration_0058_async_resident_wishes(conn)
        _migration_0058_async_resident_wishes(conn)
        conn.execute(
            "SELECT id, owner_platform_user_id, status, deliver_not_before, deliver_by "
            "FROM resident_wishes WHERE 1 = 0"
        ).fetchall()
        conn.execute(
            "SELECT id, wish_id, status, next_attempt_at, lease_expires_at "
            "FROM resident_wish_jobs WHERE 1 = 0"
        ).fetchall()
        conn.execute(
            "SELECT source, wish_id FROM character_letters WHERE 1 = 0"
        ).fetchall()


def test_candidate_naming_snapshot_is_written_once(fresh_db):
    """选名只随 INSERT 落一次；同一候选再次 ensure 不会被新名字覆盖（NAME-001）。"""
    pu = _pu("19911110050")
    universe = db.get_or_create_home_universe(platform_user_id=pu)
    template = db.create_character_template(
        source_type="operations",
        name="运营工作名",
        persona_version="v1",
        name_pool_json='["甲","乙","丙"]',
        name_pool_version="np_v1",
    )
    first = db.get_or_create_candidate_resident(
        universe_id=universe["id"],
        character_template_id=template["id"],
        template_version="v1",
        origin="preset",
        suggested_display_name="甲",
        naming_version="np_v1",
    )
    second = db.get_or_create_candidate_resident(
        universe_id=universe["id"],
        character_template_id=template["id"],
        template_version="v1",
        origin="preset",
        suggested_display_name="丙",
        naming_version="np_v2",
    )
    assert first["id"] == second["id"]
    assert second["suggested_display_name"] == "甲"
    assert second["naming_version"] == "np_v1"
