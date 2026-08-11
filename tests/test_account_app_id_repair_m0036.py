"""m0036：修复旧分支迁移编号碰撞造成的 account app_id schema 漂移。"""

import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import (
    _MIGRATIONS,
    _migration_0036_repair_account_app_id,
    migrate_db_through,
)

# 回放终点取当前 head，而不是写死版本号，避免每加一条迁移就假红一次。
_HEAD_VERSION = _MIGRATIONS[-1][0]


def _drop_post_v35_plum_schema(conn) -> None:
    """Make the head fixture structurally match the legacy v35 start point."""

    for table in (
        "plum_access_invites",
        "plum_storyline_state",
        "plum_storylines",
        "plum_connection_character_adoptions",
        "plum_connection_relationship_state",
        "plum_connection_runtime_bindings",
        "plum_character_badge_assignments",
        "plum_conversation_pins",
        "plum_character_comments",
        "plum_character_memories",
        "plum_character_likes",
        "plum_character_favorites",
        "plum_character_stats",
        "plum_user_character_relationships",
        "plum_character_bindings",
        "plum_conversations",
        "plum_connections",
        "plum_user_personas",
        "plum_character_version_tags",
        "plum_tags",
        "plum_character_versions",
        "plum_model_profiles",
        "plum_character_badges",
        "plum_characters",
        "plum_works",
        "plum_public_profiles",
        "runtime_ownerships",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def _columns(conn, table: str) -> set[str]:
    """返回当前 PostgreSQL schema 中指定表的列名。"""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (table,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def test_m0036_repairs_collided_schema_and_is_idempotent(fresh_db):
    """版本 22–24 已占用但列缺失时，m0036 应补齐 schema、数据与唯一约束。"""
    user = db.create_or_get_platform_user_by_phone(phone="13800003601")
    account = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user["id"], display_name="迁移修复测试"
    )["account"]

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO channel_bindings(account_id, channel, session_key) "
            "VALUES (?, 'app', 'legacy-app-channel')",
            (account["id"],),
        )
        conn.execute("DROP INDEX IF EXISTS ux_owner_binding_active_user_app")
        conn.execute("DROP INDEX IF EXISTS ix_accounts_app_status")
        conn.execute("ALTER TABLE account_owner_bindings DROP COLUMN app_id")
        conn.execute("ALTER TABLE accounts DROP COLUMN app_id")
        _drop_post_v35_plum_schema(conn)
        # m0037–m0046 已依赖修复后的身份 schema；模拟旧分支回放时一并移除
        # 后续版本记录，让 init_db 从 m0036 按序重放，而不是制造不可能的迁移空洞。
        conn.execute("DELETE FROM schema_migrations WHERE version >= 36")

    # 常规 init_db 已禁止既有库跨 Phase 1 contract；迁移回放测试显式走受控 API。
    migrate_db_through(target_version=_HEAD_VERSION, expected_current_version=35)

    with db.connect() as conn:
        assert "app_id" in _columns(conn, "accounts")
        assert "app_id" in _columns(conn, "account_owner_bindings")
        assert conn.execute(
            "SELECT app_id FROM accounts WHERE id = ?", (account["id"],)
        ).fetchone()["app_id"] == "zhaoxi"
        assert conn.execute(
            "SELECT app_id FROM account_owner_bindings WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["app_id"] == "zhaoxi"
        assert conn.execute(
            "SELECT channel FROM channel_bindings WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["channel"] == "native"
        assert conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"] == _HEAD_VERSION
        _migration_0036_repair_account_app_id(conn)
        _migration_0036_repair_account_app_id(conn)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, channel, display_name, app_id) "
            "VALUES ('aid_m0036_second', 'native', '第二账号', 'zhaoxi')"
        )
    with pytest.raises(IntegrityError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO account_owner_bindings("
                "platform_user_id, account_id, binding_method, status, app_id) "
                "VALUES (?, 'aid_m0036_second', 'test', 'active', 'zhaoxi')",
                (user["id"],),
            )
