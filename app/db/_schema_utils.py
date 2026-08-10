"""迁移与运行期共用的低层 schema 工具。

从 ``app/db/_core.py`` 原样搬出，未改动函数体。单独成文件是为了打破
``_core``（需要调用迁移）与 ``app/db/migrations/*``（需要这些工具）之间的循环 import：
本模块只依赖 ``app.db._backend``，不依赖任何上层。
"""
from __future__ import annotations

from typing import Optional

from app.db._backend import Connection


def _is_offline_sqlite_connection(conn: Connection) -> bool:
    """Recognize explicit SQLite connections used by legacy import tooling only."""
    return type(conn).__module__ == "sqlite3"


def product_quota_subject(*, platform_user_id: str, app_id: str) -> str:
    """构造不会与旧真人级 key 混淆的产品配额/RPM subject。"""
    cleaned_user_id = str(platform_user_id or "").strip()
    cleaned_app_id = str(app_id or "").strip()
    if not cleaned_user_id or not cleaned_app_id:
        raise ValueError("platform_user_id and app_id are required")
    return f"product:{len(cleaned_app_id)}:{cleaned_app_id}:{cleaned_user_id}"


def _table_exists(conn: Connection, table: str) -> bool:
    if _is_offline_sqlite_connection(conn):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    row = conn.execute("SELECT to_regclass(?) AS r", (table,)).fetchone()
    return row is not None and row["r"] is not None


def _ensure_column(conn: Connection, table: str, column: str, definition: str) -> None:
    if _is_offline_sqlite_connection(conn):
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        exists = column in existing
    else:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        exists = row is not None
    if not exists:
        # ALTER 无参数，PG 路径经垫片自动方言翻译（definition 多为简单类型，无需翻译）
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


__all__ = [
    "_ensure_column",
    "_table_exists",
    "product_quota_subject",
]
