"""scripts/migrate_sqlite_to_pg.py 的真 PG 迁移测试。

构造一个小型 SQLite 源库（含大额 micros 测 BIGINT、IDENTITY 表测序列重置、
账本两条测勾稽），迁移到临时 PG，校验：逐表行数对拍、账本勾稽、BIGINT 不溢出、
IDENTITY 序列对齐到 MAX(id)+1。任一依赖缺失则整文件跳过。
"""
import sqlite3

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("pytest_postgresql")

try:
    from pytest_postgresql import factories as _pg_factories

    postgresql_my_proc = _pg_factories.postgresql_proc()
    postgresql_my = _pg_factories.postgresql("postgresql_my_proc")
except Exception:  # pragma: no cover
    pytest.skip("pytest-postgresql 不可用", allow_module_level=True)

from scripts.migrate_sqlite_to_pg import migrate  # noqa: E402

# 大于 int4 上限（2_147_483_647）：验证 SQLite INTEGER → PG BIGINT 不溢出
_BIG_BALANCE = 5_000_000_000


def _dsn_from_conn(conn) -> str:
    info = conn.info
    if info.host and info.host.startswith("/"):
        return f"postgresql://{info.user}@/{info.dbname}?host={info.host}&port={info.port}"
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _build_source_sqlite(path: str) -> None:
    """用 app 的迁移函数在裸 sqlite3 连接上建全量 schema，再灌入样本数据。"""
    from app.db._core import (
        _migration_0001_baseline,
        _migration_0002_llm_runtime_config,
        _migration_0003_user_meta,
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 迁移函数的 _ensure_column 用 row["name"] 访问 PRAGMA 结果
    try:
        _migration_0001_baseline(conn)
        _migration_0002_llm_runtime_config(conn)
        _migration_0003_user_meta(conn)
        conn.execute("INSERT INTO accounts (id) VALUES (?)", ("acc1",))
        conn.execute(
            "INSERT INTO platform_users (id, phone) VALUES (?, ?)", ("pu1", "13800000000")
        )
        conn.execute(
            "INSERT INTO entitlement_wallets (id, account_id, platform_user_id, balance_shell_micros) "
            "VALUES (?, ?, ?, ?)",
            ("w1", "acc1", "pu1", _BIG_BALANCE),
        )
        # 两条账本，金额之和 == 钱包余额（用于勾稽）
        conn.execute(
            "INSERT INTO entitlement_ledger (id, wallet_id, account_id, platform_user_id, "
            "entry_type, source_type, amount_shell_micros, balance_after_shell_micros, idempotency_key) "
            "VALUES (?, ?, ?, ?, 'grant', 'new_user', ?, ?, ?)",
            ("l1", "w1", "acc1", "pu1", 3_000_000_000, 3_000_000_000, "idem-1"),
        )
        conn.execute(
            "INSERT INTO entitlement_ledger (id, wallet_id, account_id, platform_user_id, "
            "entry_type, source_type, amount_shell_micros, balance_after_shell_micros, idempotency_key) "
            "VALUES (?, ?, ?, ?, 'grant', 'referral', ?, ?, ?)",
            ("l2", "w1", "acc1", "pu1", 2_000_000_000, _BIG_BALANCE, "idem-2"),
        )
        # IDENTITY 表：不传 id，自增出 1、2（(platform_user_id, account_id) 唯一，用不同 account_id）
        for acc in ("acc1", "acc2"):
            conn.execute(
                "INSERT INTO account_owner_bindings (platform_user_id, account_id, binding_method) "
                "VALUES (?, ?, 'manual')",
                ("pu1", acc),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def pg_dsn(postgresql_my):
    from app.db._backend import close_pg_pool

    try:
        yield _dsn_from_conn(postgresql_my)
    finally:
        close_pg_pool()


def test_migrate_row_counts_and_ledger_reconcile(tmp_path, pg_dsn):
    src = str(tmp_path / "src.sqlite3")
    _build_source_sqlite(src)

    result = migrate(src, pg_dsn, batch=100)

    # 复制行数
    assert result["copied"]["accounts"] == 1
    assert result["copied"]["platform_users"] == 1
    assert result["copied"]["entitlement_wallets"] == 1
    assert result["copied"]["entitlement_ledger"] == 2
    assert result["copied"]["account_owner_bindings"] == 2

    rec = result["reconcile"]
    # 逐表行数全部一致（含大量空表 0==0）
    assert rec["ok"] is True
    assert rec["tables"]["entitlement_ledger"] == {"sqlite": 2, "pg": 2, "match": True}
    # 账本勾稽：1 个钱包，balance == SUM(amount)，无差异
    assert rec["ledger"]["wallets"] == 1
    assert rec["ledger"]["balanced"] == 1
    assert rec["ledger"]["mismatches"] == []


def test_migrate_preserves_bigint_and_resets_identity(tmp_path, pg_dsn):
    import psycopg

    src = str(tmp_path / "src.sqlite3")
    _build_source_sqlite(src)
    migrate(src, pg_dsn, batch=100)

    with psycopg.connect(pg_dsn) as pconn:
        with pconn.cursor() as cur:
            # 大额 micros 原值搬运、未溢出（BIGINT）
            cur.execute("SELECT balance_shell_micros FROM entitlement_wallets WHERE id = 'w1'")
            assert cur.fetchone()[0] == _BIG_BALANCE

            # 原始 id 1、2 已保留
            cur.execute("SELECT id FROM account_owner_bindings ORDER BY id")
            assert [r[0] for r in cur.fetchall()] == [1, 2]

            # 序列已对齐到 MAX(id)：新插入（不传 id）应得 3，不撞号
            cur.execute(
                "INSERT INTO account_owner_bindings (platform_user_id, account_id, binding_method) "
                "VALUES ('pu1', 'acc3', 'manual') RETURNING id"
            )
            assert cur.fetchone()[0] == 3
        pconn.commit()
