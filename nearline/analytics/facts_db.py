"""读写 facts.sqlite3（dim_/fct_，durable 基线，只追加）。

包含建表（执行 facts_schema.sql，幂等）与 watermark 读写辅助。

path_override 参数：传非 None 值可重定向到任意路径，供测试或 --source-db 配套使用，
避免测试数据混入生产 facts.sqlite3。
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "nearline" / "data"
FACTS_DB = DATA_DIR / "facts.sqlite3"
SCHEMA_FILE = Path(__file__).resolve().parent / "warehouse" / "facts_schema.sql"


def connect_facts(path_override: Optional[str] = None) -> sqlite3.Connection:
    """打开（必要时创建）facts 数据库读写连接。

    path_override 非 None 时使用指定路径，否则使用默认 nearline/data/facts.sqlite3。
    """
    if path_override:
        db_path = Path(path_override)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db_path = FACTS_DB
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_facts(path_override: Optional[str] = None) -> None:
    """执行 facts_schema.sql 建表（CREATE IF NOT EXISTS，可重复调用）。"""
    conn = connect_facts(path_override)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        _ensure_column(conn, "dim_account", "app_id", "TEXT NOT NULL DEFAULT 'zhaoxi'")
        _ensure_column(conn, "dim_account", "platform_user_id", "TEXT")
        _ensure_column(conn, "dim_account", "platform_user_registered_date", "TEXT")
        _ensure_column(conn, "dim_account", "product_member_registered_date", "TEXT")
        _ensure_column(conn, "fct_message", "channel", "TEXT")
        _ensure_column(conn, "fct_proactive_message", "channel", "TEXT")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """给已有 facts 表补充加性列，保持历史 durable facts 可原地升级。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def get_watermark(conn: sqlite3.Connection, table_name: str) -> int:
    """返回某 fct_ 表已装载的最大源主键；无记录返回 0。"""
    row = conn.execute(
        "SELECT last_id FROM etl_watermark WHERE table_name = ?", (table_name,)
    ).fetchone()
    return int(row["last_id"]) if row else 0


def set_watermark(conn: sqlite3.Connection, table_name: str, last_id: int) -> None:
    """记录某 fct_ 表最新装载位点（UPSERT）。"""
    conn.execute(
        "INSERT INTO etl_watermark(table_name, last_id, last_run_at) VALUES (?, ?, ?) "
        "ON CONFLICT(table_name) DO UPDATE SET "
        "last_id = excluded.last_id, last_run_at = excluded.last_run_at",
        (table_name, int(last_id), datetime.now().isoformat(timespec="seconds")),
    )
