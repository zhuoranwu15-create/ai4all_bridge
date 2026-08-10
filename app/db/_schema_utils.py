"""PostgreSQL migration 与运行期共用的低层 schema 工具。"""
from __future__ import annotations

from typing import Optional

from app.db._backend import Connection


def product_quota_subject(*, platform_user_id: str, app_id: str) -> str:
    """构造不会与旧真人级 key 混淆的产品配额/RPM subject。"""
    cleaned_user_id = str(platform_user_id or "").strip()
    cleaned_app_id = str(app_id or "").strip()
    if not cleaned_user_id or not cleaned_app_id:
        raise ValueError("platform_user_id and app_id are required")
    return f"product:{len(cleaned_app_id)}:{cleaned_app_id}:{cleaned_user_id}"


def _table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(?) AS r", (table,)).fetchone()
    return row is not None and row["r"] is not None


def _ensure_column(conn: Connection, table: str, column: str, definition: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
        (table, column),
    ).fetchone()
    if row is None:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


__all__ = [
    "_ensure_column",
    "_table_exists",
    "product_quota_subject",
]
