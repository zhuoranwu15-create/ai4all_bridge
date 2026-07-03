"""读写 marts.sqlite3（agg_ 聚合层，可删重建，不入备份）。"""

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "nearline" / "data"
MARTS_DB = DATA_DIR / "marts.sqlite3"
SCHEMA_FILE = Path(__file__).resolve().parent / "warehouse" / "marts_schema.sql"


def connect_marts() -> sqlite3.Connection:
    """打开（必要时创建）marts.sqlite3 读写连接。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MARTS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_marts() -> None:
    """执行 marts_schema.sql 建表（幂等）。"""
    conn = connect_marts()
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _ensure_column(conn, "agg_daily_proactive", "failed_count",
                       "INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """给已存在的表补列（CREATE TABLE IF NOT EXISTS 不会给旧表加新列）。幂等。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
