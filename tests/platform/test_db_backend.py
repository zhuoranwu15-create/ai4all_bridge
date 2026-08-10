"""app.db._backend PostgreSQL adapter unit tests.

覆盖：
- 占位符翻译器（纯函数，PG 路径的核心、最高复用风险点）。
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
# Migration script splitting
# ---------------------------------------------------------------------------

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
