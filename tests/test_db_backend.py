"""app.db._backend PostgreSQL 兼容层单测。

覆盖：
- 占位符翻译器（纯函数，PG 路径的核心、最高复用风险点）。
- SQLite 风格 SQL 到 PostgreSQL 的临时方言翻译。
- 主应用拒绝空 URL 和 SQLite URL，不存在本地文件回落。
"""
import types

import pytest

from app.db import _backend


# ---------------------------------------------------------------------------
# 占位符翻译器
# ---------------------------------------------------------------------------

def test_translate_basic_placeholders():
    sql = "SELECT * FROM t WHERE a = ? AND b = ?"
    assert _backend.translate_placeholders(sql) == "SELECT * FROM t WHERE a = %s AND b = %s"


def test_translate_skips_question_mark_inside_string_literal():
    sql = "SELECT 'a?b' AS x FROM t WHERE c = ?"
    # 字面量内的 ? 保留，仅 WHERE 的 ? 翻译
    assert _backend.translate_placeholders(sql) == "SELECT 'a?b' AS x FROM t WHERE c = %s"


def test_translate_escapes_literal_percent():
    sql = "SELECT * FROM t WHERE name LIKE '%foo%' AND a = ?"
    assert (
        _backend.translate_placeholders(sql)
        == "SELECT * FROM t WHERE name LIKE '%%foo%%' AND a = %s"
    )


def test_translate_handles_escaped_single_quote():
    # '' 是字符串内的转义单引号，不应翻转内/外状态；其后的 ? 仍在串内、不翻译
    sql = "SELECT 'it''s ok? yes' AS x, c = ?"
    out = _backend.translate_placeholders(sql)
    assert "'it''s ok? yes'" in out
    assert out.endswith("c = %s")


def test_translate_noop_when_no_placeholder():
    sql = "SELECT 1"
    assert _backend.translate_placeholders(sql) == "SELECT 1"


# ---------------------------------------------------------------------------
# 语句方言翻译（DDL + DML 共用）
# ---------------------------------------------------------------------------

def test_translate_statement_autoincrement():
    out = _backend.translate_statement("id INTEGER PRIMARY KEY AUTOINCREMENT,")
    assert out == "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"


def test_translate_statement_strftime_default():
    sql = "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))"
    out = _backend.translate_statement(sql)
    assert "strftime" not in out
    assert "to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD HH24:MI:SS')" in out


def test_translate_statement_strftime_in_dml_values():
    # DML 路径（INSERT VALUES 内的 now-北京默认值）也必须翻译，否则 PG 报错
    sql = (
        "INSERT INTO messages(account_id, created_at) "
        "VALUES (?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))"
    )
    out = _backend.translate_statement(sql)
    assert "strftime" not in out
    assert "to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD HH24:MI:SS')" in out
    # 占位符 ? 在本层不动（由 translate_placeholders 负责）
    assert "VALUES (?," in out


def test_translate_statement_insert_or_ignore():
    sql = "INSERT OR IGNORE INTO access_nodes(node_id, status) VALUES (?, ?)"
    out = _backend.translate_statement(sql)
    assert out == "INSERT INTO access_nodes(node_id, status) VALUES (?, ?) ON CONFLICT DO NOTHING"


def test_translate_statement_insert_or_ignore_strips_trailing_semicolon():
    sql = "INSERT OR IGNORE INTO t(a) VALUES (?);"
    out = _backend.translate_statement(sql)
    assert out == "INSERT INTO t(a) VALUES (?) ON CONFLICT DO NOTHING"


def test_translate_statement_strips_foreign_keys_and_dangling_comma():
    sql = (
        "CREATE TABLE t (\n"
        "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
        "    account_id TEXT NOT NULL,\n"
        "    session_id INTEGER NOT NULL,\n"
        "    FOREIGN KEY(account_id) REFERENCES accounts(id),\n"
        "    FOREIGN KEY(session_id) REFERENCES sessions(id)\n"
        ")"
    )
    out = _backend.translate_statement(sql)
    assert "FOREIGN KEY" not in out
    assert "REFERENCES" not in out
    # 末列后不应残留悬挂逗号
    import re as _re

    assert _re.search(r",\s*\)", out) is None
    assert "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in out


def test_translate_statement_no_fk_keeps_inline_comma_paren():
    # 不含 FOREIGN 的普通 DML 不应触发悬挂逗号清理，`, )` 这类序列原样保留
    sql = "INSERT INTO t(a, b) VALUES (1, )"
    assert _backend.translate_statement(sql) == sql


def test_translate_statement_fast_path_noop():
    sql = "SELECT a, b FROM t WHERE c = 1"
    assert _backend.translate_statement(sql) is sql


# ---------------------------------------------------------------------------
# 查询体日期函数翻译（1d.2）
# ---------------------------------------------------------------------------

_TO_CHAR_TS = "to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD HH24:MI:SS')"


def test_translate_bare_datetime_now_bj():
    out = _backend.translate_statement(
        "SELECT 1 WHERE expires_at > datetime('now', '+8 hours')"
    )
    assert out == "SELECT 1 WHERE expires_at > " + _TO_CHAR_TS


def test_translate_datetime_with_literal_modifier():
    out = _backend.translate_statement(
        "WHERE created_at > datetime('now', '+8 hours', '-1 hour')"
    )
    assert out == (
        "WHERE created_at > to_char((now() AT TIME ZONE 'Asia/Shanghai') "
        "+ ('-1 hour')::interval,'YYYY-MM-DD HH24:MI:SS')"
    )


def test_translate_datetime_with_param_modifier():
    # ? 修饰符（参数本身是完整 SQLite 修饰符串，如 '-30 minutes'）
    out = _backend.translate_statement(
        "WHERE created_at >= datetime('now', '+8 hours', ?)"
    )
    assert "+ (?)::interval" in out
    assert "datetime" not in out


def test_translate_datetime_with_concat_param_modifier():
    out = _backend.translate_statement(
        "SET token_expires_at = datetime('now', '+8 hours', ? || ' minutes')"
    )
    assert out == (
        "SET token_expires_at = to_char((now() AT TIME ZONE 'Asia/Shanghai') "
        "+ (? || ' minutes')::interval,'YYYY-MM-DD HH24:MI:SS')"
    )


def test_translate_strftime_wrapped_modifier():
    # moderation/proactive 里 strftime 包裹带修饰符的 datetime
    out = _backend.translate_statement(
        "AND claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?))"
    )
    assert "strftime" not in out
    assert "datetime" not in out
    assert "+ (?)::interval" in out


def test_translate_date_now_bj():
    out = _backend.translate_statement(
        "SELECT date('now', '+8 hours') || ' 00:00:00' AS since"
    )
    assert out == (
        "SELECT to_char((now() AT TIME ZONE 'Asia/Shanghai'),'YYYY-MM-DD') "
        "|| ' 00:00:00' AS since"
    )


def test_translate_julianday_ms_diff():
    out = _backend.translate_statement(
        "CAST(ROUND((julianday(r.created_at) - julianday(u.created_at)) * 86400000) AS INTEGER)"
    )
    assert "julianday" not in out
    assert (
        "EXTRACT(EPOCH FROM ((r.created_at)::timestamp - (u.created_at)::timestamp)) * 1000"
        in out
    )


# ---------------------------------------------------------------------------
# JSON 函数翻译
# ---------------------------------------------------------------------------

def test_translate_json_extract_single_key():
    out = _backend.translate_statement(
        "SELECT json_extract(metadata_json, '$.reactivation') FROM t"
    )
    # 原始调用形式消失，替换为 safe_json_extract_text
    assert "json_extract(metadata_json" not in out
    assert "safe_json_extract_text(metadata_json, ARRAY['reactivation'])" in out


def test_translate_json_extract_nested_path():
    out = _backend.translate_statement(
        "AND json_extract(s.metadata_json, '$.reactivation_candidate.scheduled_at') <= ?"
    )
    assert "json_extract(s.metadata_json" not in out
    assert (
        "safe_json_extract_text(s.metadata_json, ARRAY['reactivation_candidate', 'scheduled_at'])"
        in out
    )


def test_translate_json_valid_passthrough():
    # json_valid 不再被垫片层翻译；直接透传给 PG 调用注册的同名函数
    sql = "WHERE json_valid(metadata_json)"
    out = _backend.translate_statement(sql)
    assert out == sql
    assert "IS JSON" not in out


def test_translate_reactivation_json_bool_predicate_uses_text_comparison():
    sql = (
        "AND CAST(json_extract(metadata_json, '$.reactivation') AS TEXT) "
        "IN ('1', 'true')"
    )
    out = _backend.translate_statement(sql)
    assert "safe_json_extract_text(metadata_json, ARRAY['reactivation'])" in out
    assert "AS TEXT" in out
    assert "AS INTEGER" not in out


def test_translate_mixed_modifier_and_plain_in_one_statement():
    # 同一语句里既有带修饰符 datetime 又有 strftime 无修饰符，互不干扰
    sql = (
        "VALUES (datetime('now', '+8 hours', ? || ' minutes'), "
        "strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))"
    )
    out = _backend.translate_statement(sql)
    assert "datetime" not in out
    assert "strftime" not in out
    assert "+ (? || ' minutes')::interval" in out
    # 无修饰符部分仍翻成普通 to_char
    assert out.count(_TO_CHAR_TS) == 1


def test_split_sql_statements_strips_comments_and_splits():
    script = "CREATE TABLE a (x INT);  -- 注释\nCREATE INDEX i ON a(x);\n"
    assert _backend.split_sql_statements(script) == [
        "CREATE TABLE a (x INT)",
        "CREATE INDEX i ON a(x)",
    ]


# ---------------------------------------------------------------------------
# PostgreSQL-only 配置门禁
# ---------------------------------------------------------------------------

def _fake_settings(**kw):
    s = types.SimpleNamespace()
    s.database_url = kw.get("database_url", "")
    return s


@pytest.mark.parametrize("url", ["", "sqlite:///data/ai4all.sqlite3", "mysql://u:p@h/db"])
def test_database_url_rejects_non_postgres_configuration(monkeypatch, url):
    monkeypatch.setattr(
        _backend, "_settings", lambda: _fake_settings(database_url=url)
    )
    with pytest.raises(RuntimeError, match="requires PostgreSQL DATABASE_URL"):
        _backend.database_url()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/db",
        "postgres://u:p@h/db",
        "POSTGRESQL://u@h/db",
    ],
)
def test_database_url_accepts_postgres_urls(monkeypatch, url):
    monkeypatch.setattr(_backend, "_settings", lambda: _fake_settings(database_url=url))
    assert _backend.database_url() == url


def test_connect_raw_has_no_sqlite_fallback(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(_backend, "_connect_postgres", lambda: sentinel)
    assert _backend.connect_raw() is sentinel
