#!/usr/bin/env python3
"""
一次性迁移脚本：将数据库中所有由 CURRENT_TIMESTAMP DEFAULT 写入的 UTC 时间列
向前调整 +8 小时，统一为北京时间（UTC+8）。

应用层已写入北京时间的字段（scheduled_at、due_at、next_scan_at 等）不在迁移范围内。

用法：
    # Dry-run（默认）：仅打印待迁移行数和抽样值，不修改任何数据
    .venv/bin/python scripts/migrate_timestamps_to_beijing.py

    # 实际迁移：自动备份 DB 后执行 UPDATE
    .venv/bin/python scripts/migrate_timestamps_to_beijing.py --apply

    # 指定数据库路径
    .venv/bin/python scripts/migrate_timestamps_to_beijing.py --db data/ai4all.sqlite3 --apply
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# (table, column) — 仅限 DEFAULT CURRENT_TIMESTAMP（UTC）写入的字段
# 应用层明确写入北京时间的字段（scheduled_at、due_at 等）不包含在内
MIGRATION_TARGETS = [
    ("accounts", "created_at"),
    ("accounts", "updated_at"),
    ("platform_users", "created_at"),
    ("platform_users", "updated_at"),
    ("subscriptions", "created_at"),
    ("subscriptions", "updated_at"),
    ("entitlement_wallets", "created_at"),
    ("entitlement_wallets", "updated_at"),
    ("entitlement_ledger", "created_at"),
    ("cost_events", "created_at"),
    ("account_owner_bindings", "created_at"),
    ("account_owner_bindings", "updated_at"),
    ("binding_intents", "created_at"),
    ("binding_intents", "updated_at"),
    ("sessions", "created_at"),
    ("sessions", "updated_at"),
    ("profiles", "created_at"),
    ("profiles", "updated_at"),
    ("messages", "created_at"),
    ("daily_usage", "updated_at"),
    ("channel_bindings", "first_seen_at"),
    ("channel_bindings", "last_seen_at"),
    ("outbound_messages", "created_at"),
    ("outbound_messages", "updated_at"),
    ("reminders", "created_at"),
    ("reminders", "updated_at"),
    ("proactive_commitments", "created_at"),
    ("proactive_commitments", "updated_at"),
    ("proactive_account_state", "created_at"),
    ("proactive_account_state", "updated_at"),
    ("content_invitations", "created_at"),
    ("content_invitations", "updated_at"),
    ("content_invitation_preferences", "created_at"),
    ("content_invitation_preferences", "updated_at"),
    ("dreaming_runs", "created_at"),
    ("dreaming_runs", "updated_at"),
    ("dreaming_memory_items", "created_at"),
    ("memory_events", "created_at"),
    ("debug_traces", "created_at"),
    ("tool_invocations", "created_at"),
    ("tool_invocations", "updated_at"),
    ("search_provider_runs", "started_at"),
    ("search_provider_runs", "created_at"),
    ("search_provider_runs", "updated_at"),
    ("admin_users", "created_at"),
    ("admin_users", "updated_at"),
    ("admin_access_events", "created_at"),
    ("admin_plaintext_grants", "created_at"),
    ("admin_plaintext_grants", "updated_at"),
    ("phone_verifications", "created_at"),
    ("scheduler_heartbeats", "created_at"),
    ("scheduler_heartbeats", "updated_at"),
    ("faq_messages", "created_at"),
    ("faq_messages", "updated_at"),
    ("faq_message_likes", "created_at"),
    ("platform_user_sessions", "created_at"),
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _count_rows(conn: sqlite3.Connection, table: str, col: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def _sample_rows(conn: sqlite3.Connection, table: str, col: str, n: int = 3):
    rows = conn.execute(
        f"SELECT {col}, datetime({col}, '+8 hours') FROM {table} WHERE {col} IS NOT NULL LIMIT {n}"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def run(db_path: str, apply: bool) -> None:
    path = Path(db_path)
    if not path.exists():
        print(f"[ERROR] 数据库不存在：{db_path}")
        sys.exit(1)

    if apply:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_suffix(f".{ts}.bak.sqlite3")
        shutil.copy2(path, backup)
        print(f"[BACKUP] 已备份到 {backup}\n")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    total_rows = 0
    skipped_tables = []

    print(f"{'TABLE.COLUMN':<45} {'ROWS':>8}  {'SAMPLE BEFORE':>24}  →  {'SAMPLE AFTER':>24}")
    print("-" * 110)

    for table, col in MIGRATION_TARGETS:
        if not _table_exists(conn, table):
            skipped_tables.append(table)
            continue

        count = _count_rows(conn, table, col)
        total_rows += count
        samples = _sample_rows(conn, table, col)
        label = f"{table}.{col}"

        if samples:
            before, after = samples[0]
            print(f"{label:<45} {count:>8}  {str(before):>24}  →  {str(after):>24}")
        else:
            print(f"{label:<45} {count:>8}  (无数据)")

        if apply and count > 0:
            conn.execute(
                f"UPDATE {table} SET {col} = datetime({col}, '+8 hours') WHERE {col} IS NOT NULL"
            )

    if apply:
        conn.commit()
        print(f"\n[OK] 已迁移 {total_rows} 行，共 {len(MIGRATION_TARGETS)} 列")
    else:
        print(f"\n[DRY-RUN] 共 {total_rows} 行待迁移（未修改）。使用 --apply 执行实际迁移。")

    if skipped_tables:
        unique_skipped = sorted(set(skipped_tables))
        print(f"[SKIP] 以下表不存在（跳过）：{', '.join(unique_skipped)}")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移数据库时间戳：UTC → 北京时间（+8h）")
    parser.add_argument(
        "--db",
        default="data/ai4all.sqlite3",
        help="SQLite 数据库路径（默认：data/ai4all.sqlite3）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行实际迁移（默认为 dry-run）",
    )
    args = parser.parse_args()
    run(args.db, args.apply)


if __name__ == "__main__":
    main()
