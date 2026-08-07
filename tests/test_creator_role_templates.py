"""用户自建角色模板 CRT-01：schema、领域校验、owner 隔离和并发槽位。"""
from __future__ import annotations

import concurrent.futures
import sqlite3

import pytest

import app.db as db
from app.db._backend import IntegrityError, is_postgres
from app.db._core import (
    _MIGRATIONS,
    _migration_0013_campaign_codes,
    _migration_0059_creator_role_templates,
    _migration_0060_creator_role_template_opening_and_summary,
    _migration_0064_fibre_mvp,
    _migration_0065_fibre_character_experience,
)
from app.products.zhaoxi.domain.creator_role_templates import (
    AI_NAME_MAX_CHARS,
    MISSION_MAX_CHARS,
    PERSONALITY_MAX_CHARS,
    CreatorRoleTemplateError,
    is_creator_role_template_campaign_code,
    new_creator_role_template_campaign_code,
    normalize_creator_role_template_content,
)
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    create_creator_role_template,
    get_creator_role_template,
    get_creator_role_template_version,
    list_creator_role_template_events,
    list_creator_role_templates,
    soft_delete_creator_role_template,
)

_TABLES = (
    "creator_role_templates",
    "creator_role_template_versions",
    "creator_role_template_review_runs",
    "account_creator_role_template_attribution",
    "creator_role_template_events",
    "creator_role_template_summary_review_runs",
)

_INDEXES = {
    "ux_creator_role_templates_owner_slot_live",
    "ux_creator_role_templates_campaign_code",
    "ux_creator_role_template_versions_published",
    "ux_creator_role_template_versions_open_review",
    "ix_creator_role_template_summary_runs_version",
}


def _seed_platform_user(user_id: str, phone: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO platform_users(id, phone) VALUES (?, ?)", (user_id, phone)
        )


def _create(owner_id: str, suffix: str = ""):
    return create_creator_role_template(
        creator_platform_user_id=owner_id,
        app_id="zhaoxi",
        ai_name=f"朝朝{suffix}",
        personality_text="温柔、坦诚，也会在重要时刻提醒边界。",
        mission_text="陪伴用户更清楚地看见自己，并把想法落实到生活里。",
        opening_line="我是朝朝，很高兴认识你。以后想聊什么都可以告诉我。",
    )


def test_creator_role_schema_indexes_current_head_and_idempotency(fresh_db):
    assert _MIGRATIONS[-1] == (
        65,
        _migration_0065_fibre_character_experience,
    )
    with db.connect() as conn:
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
        assert int(version) == 65
        for table in _TABLES:
            conn.execute(f"SELECT 1 FROM {table} WHERE 1 = 0").fetchall()

        if is_postgres():
            rows = conn.execute(
                "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        assert _INDEXES.issubset({row["name"] for row in rows})

        _migration_0059_creator_role_templates(conn)
        _migration_0059_creator_role_templates(conn)
        _migration_0060_creator_role_template_opening_and_summary(conn)
        _migration_0060_creator_role_template_opening_and_summary(conn)


def test_migration_59_fails_closed_for_existing_reserved_operator_code():
    conn = sqlite3.connect(":memory:")
    try:
        _migration_0013_campaign_codes(conn)
        conn.execute(
            "INSERT INTO campaign_codes(id, code, campaign_key) VALUES ('c1', 'UrT_old', 'x')"
        )
        with pytest.raises(RuntimeError, match="reserved urt_ prefix"):
            _migration_0059_creator_role_templates(conn)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'creator_role_templates'"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_migration_60_upgrades_existing_migration_59_rows_compatibly():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE creator_role_template_versions (
                id TEXT PRIMARY KEY,
                creator_role_template_id TEXT NOT NULL,
                ai_name TEXT NOT NULL
            );
            CREATE TABLE account_creator_role_template_attribution (
                account_id TEXT PRIMARY KEY,
                creator_role_template_version_id TEXT NOT NULL
            );
            INSERT INTO creator_role_template_versions(
                id, creator_role_template_id, ai_name
            ) VALUES ('v-old', 't-old', '旧角色');
            INSERT INTO account_creator_role_template_attribution(
                account_id, creator_role_template_version_id
            ) VALUES ('a-old', 'v-old');
            """
        )
        _migration_0060_creator_role_template_opening_and_summary(conn)
        _migration_0060_creator_role_template_opening_and_summary(conn)

        version = conn.execute(
            "SELECT opening_line, generated_summary, public_summary, "
            "summary_edit_status FROM creator_role_template_versions "
            "WHERE id = 'v-old'"
        ).fetchone()
        assert dict(version) == {
            "opening_line": None,
            "generated_summary": None,
            "public_summary": None,
            "summary_edit_status": "unavailable",
        }
        attribution = conn.execute(
            "SELECT opening_line_snapshot "
            "FROM account_creator_role_template_attribution "
            "WHERE account_id = 'a-old'"
        ).fetchone()
        assert attribution["opening_line_snapshot"] is None
        conn.execute(
            "SELECT 1 FROM creator_role_template_summary_review_runs WHERE 1 = 0"
        ).fetchall()
    finally:
        conn.close()


def test_domain_validates_all_four_fields_and_generated_code_entropy():
    content = normalize_creator_role_template_content(
        ai_name=" 朝朝 ",
        personality_text="第一层底色\n第二层底色",
        mission_text="陪用户找到自己的节奏",
        opening_line="我是朝朝，很高兴认识你。",
    )
    assert content.ai_name == "朝朝"
    assert "\n" in content.personality_text

    invalid_cases = (
        {"ai_name": "", "personality_text": "性格", "mission_text": "使命"},
        {
            "ai_name": "名" * (AI_NAME_MAX_CHARS + 1),
            "personality_text": "性格",
            "mission_text": "使命",
        },
        {"ai_name": "朝\n朝", "personality_text": "性格", "mission_text": "使命"},
        {"ai_name": "朝朝", "personality_text": "性\x00格", "mission_text": "使命"},
        {
            "ai_name": "朝朝",
            "personality_text": "性" * (PERSONALITY_MAX_CHARS + 1),
            "mission_text": "使命",
        },
        {
            "ai_name": "朝朝",
            "personality_text": "性格",
            "mission_text": "使" * (MISSION_MAX_CHARS + 1),
        },
    )
    for fields in invalid_cases:
        with pytest.raises(CreatorRoleTemplateError):
            normalize_creator_role_template_content(
                opening_line="我是朝朝，很高兴认识你。",
                **fields,
            )

    codes = {new_creator_role_template_campaign_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(is_creator_role_template_campaign_code(code) for code in codes)
    assert all(code.startswith("urt_") and len(code) >= 26 for code in codes)


def test_slot_limit_owner_isolation_and_soft_delete_reuse(fresh_db):
    _seed_platform_user("pu_creator_a", "13800001001")
    _seed_platform_user("pu_creator_b", "13800001002")

    created = [_create("pu_creator_a", str(index)) for index in range(1, 4)]
    assert [item.template.slot_no for item in created] == [1, 2, 3]
    assert all(item.version.version_no == 1 for item in created)
    assert all(item.version.review_status == "pending" for item in created)

    with pytest.raises(CreatorRoleTemplateError) as exc_info:
        _create("pu_creator_a", "4")
    assert exc_info.value.code == "creator_role_template_limit_reached"

    target = created[1]
    assert (
        get_creator_role_template(
            creator_platform_user_id="pu_creator_b",
            app_id="zhaoxi",
            template_id=target.template.id,
        )
        is None
    )
    assert (
        get_creator_role_template(
            creator_platform_user_id="pu_creator_a",
            app_id="other_app",
            template_id=target.template.id,
        )
        is None
    )
    assert (
        get_creator_role_template_version(
            creator_platform_user_id="pu_creator_b",
            app_id="zhaoxi",
            template_id=target.template.id,
            version_id=target.version.id,
        )
        is None
    )
    assert list_creator_role_template_events(
        creator_platform_user_id="pu_creator_b",
        app_id="zhaoxi",
        template_id=target.template.id,
    ) == []

    deleted = soft_delete_creator_role_template(
        creator_platform_user_id="pu_creator_a",
        app_id="zhaoxi",
        template_id=target.template.id,
    )
    assert deleted.status == "deleted"
    assert deleted.deleted_at is not None
    assert len(list_creator_role_templates(
        creator_platform_user_id="pu_creator_a", app_id="zhaoxi"
    )) == 2
    assert len(list_creator_role_templates(
        creator_platform_user_id="pu_creator_a",
        app_id="zhaoxi",
        include_deleted=True,
    )) == 3

    replacement = _create("pu_creator_a", "replacement")
    assert replacement.template.slot_no == 2
    events = list_creator_role_template_events(
        creator_platform_user_id="pu_creator_a",
        app_id="zhaoxi",
        template_id=target.template.id,
        include_deleted_template=True,
    )
    assert {event["event_type"] for event in events} == {"created", "deleted"}


def test_campaign_code_collision_retries_without_consuming_slot(fresh_db, monkeypatch):
    _seed_platform_user("pu_code_retry", "13800001003")
    first = _create("pu_code_retry", "first")
    generated = iter(
        (first.template.campaign_code, "urt_" + "A" * 32, "urt_" + "A" * 32)
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.infrastructure.persistence.creator_role_templates."
        "new_creator_role_template_campaign_code",
        lambda: next(generated),
    )

    second = _create("pu_code_retry", "second")
    assert second.template.slot_no == 2
    assert second.template.campaign_code == "urt_" + "A" * 32


def test_campaign_code_collision_exhaustion_has_stable_error(fresh_db, monkeypatch):
    _seed_platform_user("pu_code_exhausted", "13800001006")
    first = _create("pu_code_exhausted", "first")
    monkeypatch.setattr(
        "app.products.zhaoxi.infrastructure.persistence.creator_role_templates."
        "new_creator_role_template_campaign_code",
        lambda: first.template.campaign_code,
    )

    with pytest.raises(CreatorRoleTemplateError) as exc_info:
        _create("pu_code_exhausted", "second")
    assert exc_info.value.code == "creator_role_template_code_generation_failed"
    assert len(list_creator_role_templates(
        creator_platform_user_id="pu_code_exhausted", app_id="zhaoxi"
    )) == 1


def test_partial_unique_indexes_enforce_published_and_open_review(fresh_db):
    _seed_platform_user("pu_version_unique", "13800001004")
    created = _create("pu_version_unique")
    now = created.version.created_at

    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO creator_role_template_versions(
                    id, creator_role_template_id, version_no, ai_name,
                    personality_text, mission_text, review_status,
                    is_published, created_at, updated_at
                ) VALUES ('crtv_open_2', ?, 2, '二号', '性格', '使命', 'reviewing', 0, ?, ?)
                """,
                (created.template.id, now, now),
            )

    with db.connect() as conn:
        conn.execute(
            "UPDATE creator_role_template_versions "
            "SET review_status = 'passed', is_published = 1 WHERE id = ?",
            (created.version.id,),
        )
        conn.execute(
            """
            INSERT INTO creator_role_template_versions(
                id, creator_role_template_id, version_no, ai_name,
                personality_text, mission_text, review_status,
                is_published, created_at, updated_at
            ) VALUES ('crtv_passed_2', ?, 2, '二号', '性格', '使命', 'passed', 0, ?, ?)
            """,
            (created.template.id, now, now),
        )

    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "UPDATE creator_role_template_versions SET is_published = 1 "
                "WHERE id = 'crtv_passed_2'"
            )


def test_concurrent_creation_never_exceeds_three_live_slots(fresh_db):
    _seed_platform_user("pu_concurrent_slots", "13800001005")

    def attempt(index: int):
        try:
            return _create("pu_concurrent_slots", str(index)).template.slot_no
        except CreatorRoleTemplateError as exc:
            return exc.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(attempt, range(6)))

    slots = sorted(value for value in results if isinstance(value, int))
    errors = [value for value in results if isinstance(value, str)]
    assert slots == [1, 2, 3]
    assert errors == ["creator_role_template_limit_reached"] * 3
    assert len(list_creator_role_templates(
        creator_platform_user_id="pu_concurrent_slots", app_id="zhaoxi"
    )) == 3
