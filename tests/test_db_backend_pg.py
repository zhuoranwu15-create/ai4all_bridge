"""app.db._backend 垫片层的 PostgreSQL 路径测试（真实临时 PG）。

复用主测试基座的 pytest-postgresql 临时 PG 和逐测试模板克隆库。

这里只验证**垫片机制**（连接包装、占位符翻译、HybridRow、事务、IntegrityError），
不涉及业务 schema —— PG 基线 DDL 重写是后续增量，故用手写的 PG 兼容小表。
"""
import pytest

from app.db import _backend  # noqa: E402

def _dsn_from_conn(conn) -> str:
    """从 pytest-postgresql 给的 psycopg 连接推出 URL 形式 DSN（供 is_postgres 识别）。"""
    info = conn.info
    user = info.user
    host = info.host
    port = info.port
    dbname = info.dbname
    # 本地 unix socket（host 以 / 开头）必须放进 query，URL 主体留空 host。
    if host and host.startswith("/"):
        return f"postgresql://{user}@/{dbname}?host={host}&port={port}"
    return f"postgresql://{user}@{host}:{port}/{dbname}"


@pytest.fixture
def pg_settings(postgresql_db, monkeypatch):
    """把 _backend 的 settings 指向临时 PG。"""
    import types

    dsn = _dsn_from_conn(postgresql_db)
    fake = types.SimpleNamespace(database_url=dsn, database_path="unused.sqlite3")
    monkeypatch.setattr(_backend, "_settings", lambda: fake)
    try:
        yield fake
    finally:
        _backend.close_pg_pool()


def test_pg_backend_selected(pg_settings):
    assert _backend.database_url() == pg_settings.database_url


def test_pg_roundtrip_placeholder_and_hybrid_row(pg_settings):
    from app.db._core import connect

    with connect() as conn:
        conn.execute(
            "CREATE TABLE t (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT)"
        )
        conn.execute("INSERT INTO t (name) VALUES (?)", ("hello",))
    # 独立连接读取已提交数据，验证占位符翻译 + HybridRow 双访问
    with connect() as conn:
        row = conn.execute("SELECT id, name FROM t WHERE name = ?", ("hello",)).fetchone()
        assert row["name"] == "hello"
        assert row[1] == "hello"
        assert "name" in row
        assert dict(row)["name"] == "hello"


def test_pg_literal_percent_escaped(pg_settings):
    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE t (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT)")
        for nm in ("alpha", "beta", "alfread"):
            conn.execute("INSERT INTO t (name) VALUES (?)", (nm,))
    with connect() as conn:
        # 字面 % 必须被转义，否则 psycopg 会把它当占位符前缀报错
        rows = conn.execute(
            "SELECT name FROM t WHERE name LIKE '%al%' AND id >= ? ORDER BY name", (1,)
        ).fetchall()
        names = sorted(r["name"] for r in rows)
        assert names == ["alfread", "alpha"]


def test_pg_commit_and_rollback(pg_settings):
    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE t (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES (?)", ("a",))
    # 异常路径回滚
    with pytest.raises(RuntimeError):
        with connect() as conn:
            conn.execute("INSERT INTO t (v) VALUES (?)", ("b",))
            raise RuntimeError("boom")
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_pg_integrity_error_caught_by_alias(pg_settings):
    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        conn.execute("INSERT INTO t (id) VALUES (?)", (1,))
    caught = False
    try:
        with connect() as conn:
            conn.execute("INSERT INTO t (id) VALUES (?)", (1,))
    except _backend.IntegrityError:
        caught = True
    assert caught


def test_pg_reactivation_json_bool_predicate_counts_without_integer_cast(pg_settings):
    from app.db._core import _ensure_pg_functions, connect

    with connect() as conn:
        _ensure_pg_functions(conn)
        conn.execute(
            """
            CREATE TABLE test_outbound_messages (
                account_id TEXT NOT NULL,
                quota_date TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO test_outbound_messages(account_id, quota_date, status, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            ("acc-pg-react", "2026-06-24", "sent", '{"reactivation": true}'),
        )

    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM test_outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ('pending', 'sending', 'sent')
              AND json_valid(metadata_json)
              AND CAST(json_extract(metadata_json, '$.reactivation') AS TEXT) IN ('1', 'true')
            """,
            ("acc-pg-react", "2026-06-24"),
        ).fetchone()

    assert row["count"] == 1


# ---------------------------------------------------------------------------
# 1c：真 PG 上 init_db() 建全量 schema
# ---------------------------------------------------------------------------

def test_pg_init_db_builds_full_schema(pg_settings):
    """init_db() 在真 PG 上跑通三条迁移、建出全量表（含前向外键引用的表）。"""
    from app.db._core import _MIGRATIONS, connect, init_db

    init_db()

    with connect() as conn:
        # 迁移版本写入 schema_migrations（取代 PRAGMA user_version）
        versions = [r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()]
        assert versions == [version for version, _ in _MIGRATIONS]

        # 关键表都建出来了，含 baseline 中"被先定义的表引用"的 tool_invocations
        for table in ("accounts", "messages", "sessions", "tool_invocations",
                      "entitlement_ledger", "content_invitations", "account_user_meta",
                      "account_profile_files", "rpm_hits", "creator_role_templates",
                      "creator_role_template_versions",
                      "creator_role_template_review_runs",
                      "account_creator_role_template_attribution",
                      "creator_role_template_events"):
            row = conn.execute("SELECT to_regclass(?) AS r", (table,)).fetchone()
            assert row["r"] is not None, f"表未建出: {table}"


def test_pg_init_db_idempotent(pg_settings):
    """重复 init_db() 不重复执行迁移（版本表已记录）。"""
    from app.db._core import _MIGRATIONS, connect, init_db

    init_db()
    init_db()  # 第二次应为 no-op
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(_MIGRATIONS)


def test_pg_init_db_blocks_automatic_phase1_contract_migrations(pg_settings):
    """既有 PG 库不得由常规启动越过需 drain/reconcile 的 contract。"""
    from app.db._core import connect, init_db

    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 40")

    with pytest.raises(
        RuntimeError, match="automatic Phase 1 contract migration blocked"
    ):
        init_db()

    with connect() as conn:
        version = conn.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()["version"]
    assert int(version) == 39

    with connect() as conn:
        conn.execute("DELETE FROM schema_migrations")
    with pytest.raises(
        RuntimeError, match="automatic Phase 1 contract migration blocked"
    ):
        init_db()


def test_pg_identity_default_and_now_default(pg_settings):
    """AUTOINCREMENT→IDENTITY、strftime→to_char 默认值在真 PG 生效。"""
    from app.db._core import connect, init_db

    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO accounts (id) VALUES (?)", ("aid_test_1",))
        row = conn.execute(
            "SELECT id, created_at FROM accounts WHERE id = ?", ("aid_test_1",)
        ).fetchone()
        assert row["id"] == "aid_test_1"
        # created_at 由 to_char(now()...) 默认值生成，形如 2026-06-20 12:34:56
        import re as _re

        assert _re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", row["created_at"])

        # IDENTITY 自增主键：不传 id，自动生成
        conn.execute(
            "INSERT INTO account_owner_bindings "
            "(platform_user_id, account_id, binding_method) VALUES (?, ?, ?)",
            ("pu_1", "aid_test_1", "manual"),
        )
        bid = conn.execute(
            "SELECT id FROM account_owner_bindings WHERE account_id = ?", ("aid_test_1",)
        ).fetchone()["id"]
        assert isinstance(bid, int) and bid >= 1


# ---------------------------------------------------------------------------
# 1d：运行期方言（lastrowid / INSERT OR IGNORE / DML 内 strftime）
# ---------------------------------------------------------------------------

def test_pg_lastrowid_via_lastval(pg_settings):
    """cursor.lastrowid 在 PG 上用 lastval() 复刻：返回 IDENTITY INSERT 的新 id。"""
    from app.db._core import connect

    with connect() as conn:
        conn.execute(
            "CREATE TABLE t (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v TEXT)"
        )
    with connect() as conn:
        cur = conn.execute("INSERT INTO t (v) VALUES (?)", ("a",))
        first = int(cur.lastrowid)
        cur = conn.execute("INSERT INTO t (v) VALUES (?)", ("b",))
        second = int(cur.lastrowid)
        assert second == first + 1
        # lastrowid 指向最后一次自增插入的行
        row = conn.execute("SELECT v FROM t WHERE id = ?", (second,)).fetchone()
        assert row["v"] == "b"


def test_pg_insert_or_ignore_dedup(pg_settings):
    """INSERT OR IGNORE → ON CONFLICT DO NOTHING：唯一冲突时静默跳过、不报错不重复。"""
    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO t (k, v) VALUES (?, ?)", ("k1", "first"))
        # 同主键再插：应被忽略，原值保留
        conn.execute("INSERT OR IGNORE INTO t (k, v) VALUES (?, ?)", ("k1", "second"))
    with connect() as conn:
        rows = conn.execute("SELECT k, v FROM t").fetchall()
        assert len(rows) == 1
        assert rows[0]["v"] == "first"


def test_pg_strftime_default_in_dml_values(pg_settings):
    """带参 INSERT 的 VALUES 内 strftime(now+8h) 在 PG 上被翻译为 to_char 并生效。"""
    import re as _re

    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE t (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT, ts TEXT)")
    with connect() as conn:
        conn.execute(
            "INSERT INTO t (name, ts) VALUES (?, "
            "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))",
            ("x",),
        )
    with connect() as conn:
        ts = conn.execute("SELECT ts FROM t WHERE name = ?", ("x",)).fetchone()["ts"]
        assert _re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", ts)


# ---------------------------------------------------------------------------
# 1d.2：查询体日期函数（窗口/边界/毫秒差）在真 PG 上的行为
# ---------------------------------------------------------------------------

def _seed_window_table(connect):
    """建一张 TEXT created_at 表：一行「现在」、一行远古，供窗口过滤断言。"""
    with connect() as conn:
        conn.execute("CREATE TABLE w (name TEXT, created_at TEXT)")
        # recent：用 now-北京默认值；old：远古字面值
        conn.execute(
            "INSERT INTO w (name, created_at) VALUES "
            "(?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))",
            ("recent",),
        )
        conn.execute(
            "INSERT INTO w (name, created_at) VALUES (?, ?)",
            ("old", "2000-01-01 00:00:00"),
        )


def test_pg_datetime_window_literal_modifier(pg_settings):
    """created_at >= datetime('now','+8h','-1 hour') 仅命中近窗行（TEXT 字典序=时间序）。"""
    from app.db._core import connect

    _seed_window_table(connect)
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM w WHERE created_at >= datetime('now', '+8 hours', '-1 hour')"
        ).fetchall()
        assert {r["name"] for r in rows} == {"recent"}


def test_pg_datetime_window_param_modifier(pg_settings):
    """datetime('now','+8h', ?) 参数为完整 SQLite 修饰符串，PG 当作 interval。"""
    from app.db._core import connect

    _seed_window_table(connect)
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM w WHERE created_at >= datetime('now', '+8 hours', ?)",
            ("-60 minutes",),
        ).fetchall()
        assert {r["name"] for r in rows} == {"recent"}


def test_pg_date_now_midnight_boundary(pg_settings):
    """date('now','+8h') || ' 00:00:00' 当日零点边界：近窗行入、远古行出。"""
    from app.db._core import connect

    _seed_window_table(connect)
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM w WHERE created_at >= date('now', '+8 hours') || ' 00:00:00'"
        ).fetchall()
        assert {r["name"] for r in rows} == {"recent"}


def test_pg_julianday_ms_diff(pg_settings):
    """(julianday(a)-julianday(b))*86400000 → EXTRACT(EPOCH...)*1000，毫秒差正确。"""
    from app.db._core import connect

    with connect() as conn:
        conn.execute("CREATE TABLE j (a TEXT, b TEXT)")
        conn.execute(
            "INSERT INTO j (a, b) VALUES (?, ?)",
            ("2020-01-01 00:00:01", "2020-01-01 00:00:00"),
        )
    with connect() as conn:
        ms = conn.execute(
            "SELECT CAST(ROUND((julianday(a) - julianday(b)) * 86400000) AS INTEGER) AS d FROM j"
        ).fetchone()["d"]
        assert int(ms) == 1000
