"""m0036：修复旧分支迁移编号碰撞造成的 account app_id schema 漂移。"""

import pytest

import app.db as db
from app.db._backend import IntegrityError, is_postgres
from app.db._core import _migration_0036_repair_account_app_id, migrate_db_through


def _columns(conn, table: str) -> set[str]:
    """返回指定表的列名，兼容 SQLite 与 PostgreSQL 测试后端。"""
    if is_postgres():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def test_m0036_repairs_collided_schema_and_is_idempotent(fresh_db):
    """版本 22–24 已占用但列缺失时，m0036 应补齐 schema、数据与唯一约束。"""
    user = db.create_or_get_platform_user_by_phone(phone="13800003601")
    account = db.create_ai4all_account_for_user(
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
        # m0037–m0046 已依赖修复后的身份 schema；模拟旧分支回放时一并移除
        # 后续版本记录，让 init_db 从 m0036 按序重放，而不是制造不可能的迁移空洞。
        conn.execute("DELETE FROM schema_migrations WHERE version >= 36")

    if is_postgres():
        # 常规 PG init_db 已禁止既有库跨 Phase 1 contract；迁移回放测试显式走受控 API。
        migrate_db_through(target_version=46, expected_current_version=35)
    else:
        db.init_db()

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
        ).fetchone()["version"] == 47
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
