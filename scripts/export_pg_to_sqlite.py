"""把中心 PostgreSQL 的「操作库表」快照导出成一个 SQLite 文件，供 nearline 日报读取。

背景：厚节点改造后操作库切到 PG，而 nearline 分析层（etl/quality/metrics）全是 SQLite
方言、且 `connect_source` 只会打开本地 SQLite 文件。切 PG 后那个 SQLite 文件已冻结，日报
会读到陈旧/空数据。本脚本作为「夜间 PG→SQLite 快照」桥：cron 在 run_daily 之前先跑它，
把 nearline 需要的少数几张操作表从 PG 导成一份新 SQLite，再让 run_daily 经
`NEARLINE_SOURCE_DB` 指向该快照。nearline 全部 SQLite 方言代码因此零改动。

只读 PG、只写目标 SQLite；原子落盘（先写 .tmp 再 rename）。仅在 PG 模式有意义；
SQLite 模式下 nearline 本就读活库，运行本脚本会快速失败提醒。

用法：
  .venv/bin/python scripts/export_pg_to_sqlite.py --dest nearline/data/source_snapshot.sqlite3
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db._backend import is_postgres  # noqa: E402

# nearline 经 source 连接读取的操作库表（与 nearline/analytics/* 的 FROM/JOIN 对齐）。
# 新增被 nearline 读取的操作表时，必须同步加进这里，否则日报读不到该表。
SOURCE_TABLES = (
    "accounts",
    "daily_usage",
    "dreaming_memory_items",
    "dreaming_runs",
    "messages",
    "outbound_messages",
    "scheduler_heartbeats",
    "sessions",
)


def _coerce(value):
    """把 PG 返回的单元格值规整成 SQLite 可存的存储类。

    本库 schema 用 TEXT 存时间戳/JSON、INTEGER 存布尔，PG 取回多为 str/int/None，
    直接可用；对极少数非常规类型（datetime/Decimal/bool 等）做兜底转换，保证
    nearline 的 DATE()/SUM()/`= 1` 等查询语义不变（时间戳保持 TEXT、布尔保持 0/1）。
    """
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):  # 注意：bool 是 int 子类，前面 int 分支已覆盖，此处冗余保护
        return int(value)
    return str(value)


def export(database_url: str, dest_path: Path, tables: List[str]) -> dict:
    """从 PG 导出指定表到 dest_path 指向的 SQLite 文件，返回各表行数。原子落盘。"""
    import psycopg  # 惰性：仅 PG 导出需要

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    counts: dict = {}
    pg = psycopg.connect(database_url)
    try:
        sq = sqlite3.connect(str(tmp_path))
        try:
            for table in tables:
                cur = pg.execute(f"SELECT * FROM {table}")  # 表名为受信常量，无注入面
                cols = [c.name for c in cur.description]
                rows = cur.fetchall()
                col_defs = ", ".join(f'"{c}"' for c in cols)
                # 列不声明类型 → SQLite 动态类型，存储类随插入值（int→INTEGER、str→TEXT），
                # 恰好匹配 nearline 对该列的使用方式。
                sq.execute(f'CREATE TABLE "{table}" ({col_defs})')
                if rows:
                    placeholders = ", ".join("?" * len(cols))
                    sq.executemany(
                        f'INSERT INTO "{table}" VALUES ({placeholders})',
                        [tuple(_coerce(v) for v in row) for row in rows],
                    )
                counts[table] = len(rows)
            # 记录快照元信息，便于排查陈旧（监控可读此表的 created_at）。
            sq.execute('CREATE TABLE _snapshot_meta (key TEXT PRIMARY KEY, value TEXT)')
            sq.executemany(
                'INSERT INTO _snapshot_meta(key, value) VALUES (?, ?)',
                [
                    ("exported_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
                    ("source", "postgresql"),
                    ("table_count", str(len(tables))),
                ],
            )
            sq.commit()
        finally:
            sq.close()
    finally:
        pg.close()

    os.replace(tmp_path, dest_path)
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PG→SQLite 操作库快照（供 nearline 日报）")
    parser.add_argument("--dest", default="nearline/data/source_snapshot.sqlite3",
                        help="目标 SQLite 快照路径（默认 nearline/data/source_snapshot.sqlite3）")
    parser.add_argument("--database-url", default=None,
                        help="覆盖 PG DSN，默认取 settings.database_url（从 .env 读）")
    args = parser.parse_args(argv)

    database_url = args.database_url or settings.database_url
    if not (database_url or "").lower().startswith(("postgres://", "postgresql://")):
        print("DATABASE_URL 非 PostgreSQL：无需导出，nearline 应直接读 SQLite 操作库。",
              file=sys.stderr)
        return 2

    dest_path = Path(args.dest)
    if not dest_path.is_absolute():
        dest_path = ROOT / dest_path

    try:
        counts = export(database_url, dest_path, list(SOURCE_TABLES))
    except Exception as err:
        # best-effort 飞书告警（与 nearline 一致），失败也要非零退出阻断后续 run_daily。
        try:
            from nearline.alerting import send_alert

            send_alert(f"nearline 源快照导出失败（PG→SQLite）：{err}")
        except Exception as alert_err:
            print(f"failed to send export alert: {alert_err}", file=sys.stderr)
        print(f"export failed: {err}", file=sys.stderr)
        return 1

    total = sum(counts.values())
    print(f"export ok: {dest_path} tables={len(counts)} rows={total} detail={counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
