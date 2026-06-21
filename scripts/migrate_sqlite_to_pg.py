#!/usr/bin/env python3
"""SQLite → PostgreSQL 数据迁移（厚节点改造 Phase 1，见 docs/tech_design/thick_node_postgres_refactor.md §4.5）。

流程：
1. 在目标空 PG 上按 `_MIGRATIONS` 建好全量 schema（复用 app.db.init_db，含 json_patch 函数）。
2. 逐表 `SELECT *` from SQLite → 批量 INSERT 到 PG，时间戳/JSON/布尔原值搬运。
   IDENTITY 列（SQLite AUTOINCREMENT → PG GENERATED ALWAYS）必须 `OVERRIDING SYSTEM VALUE`
   才能写入原始 id，迁移后再 `setval` 把序列对齐到 MAX(id)，避免后续自增撞号。
3. 对拍：逐表行数；账本按 wallet 勾稽 SUM(amount_shell_micros) 与 wallet balance。

用法：
    .venv/bin/python scripts/migrate_sqlite_to_pg.py \\
        --sqlite data/ai4all.sqlite3 \\
        --pg "postgresql://ai4all@/ai4all?host=/tmp&port=5432" \\
        [--batch 500] [--truncate]

仅迁移源库与目标库**同时存在**的表；源库的历史遗留表（如 contacts）当前 schema 不含，自动跳过。
schema_migrations 由 init_db 在 PG 侧建立，不从源库复制。
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Set

# 允许从仓库根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# init_db 建好的版本表，数据不从源库搬（避免主键冲突）
_SKIP_TABLES = frozenset({"schema_migrations", "sqlite_sequence"})


def _qi(name: str) -> str:
    """双引号包裹标识符（列/表名均为小写 snake_case，包裹后仍小写、且避开保留字如 date）。"""
    return '"' + name.replace('"', '""') + '"'


def discover_sqlite_tables(sconn: sqlite3.Connection) -> List[str]:
    rows = sconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in _SKIP_TABLES]


def sqlite_columns(sconn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in sconn.execute(f"PRAGMA table_info({_qi(table)})").fetchall()]


def pg_tables(pconn) -> Set[str]:
    with pconn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        return {r[0] for r in cur.fetchall()}


def pg_identity_columns(pconn, table: str) -> Set[str]:
    with pconn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND is_identity = 'YES'",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def copy_table(
    sconn: sqlite3.Connection,
    pconn,
    table: str,
    *,
    batch: int = 500,
    truncate: bool = False,
) -> int:
    """把一张表的数据从 SQLite 搬到 PG，返回搬运行数。"""
    cols = sqlite_columns(sconn, table)
    if not cols:
        return 0
    identity = pg_identity_columns(pconn, table)
    overriding = " OVERRIDING SYSTEM VALUE" if identity else ""
    col_sql = ", ".join(_qi(c) for c in cols)
    ph = ", ".join(["%s"] * len(cols))
    insert_sql = f"INSERT INTO {_qi(table)} ({col_sql}){overriding} VALUES ({ph})"

    src = sconn.execute(f"SELECT {col_sql} FROM {_qi(table)}")

    total = 0
    with pconn.cursor() as cur:
        if truncate:
            cur.execute(f"TRUNCATE {_qi(table)} RESTART IDENTITY CASCADE")
        while True:
            rows = src.fetchmany(batch)
            if not rows:
                break
            cur.executemany(insert_sql, [tuple(r) for r in rows])
            total += len(rows)
    pconn.commit()
    if identity:
        _reset_identity(pconn, table, identity)
    return total


def _reset_identity(pconn, table: str, identity_cols: Set[str]) -> None:
    """把 IDENTITY 序列对齐到 MAX(列)，使后续自增从原始最大值之后继续。"""
    with pconn.cursor() as cur:
        for col in identity_cols:
            cur.execute(
                "SELECT setval("
                "  pg_get_serial_sequence(%s, %s),"
                f"  (SELECT COALESCE(MAX({_qi(col)}), 1) FROM {_qi(table)}),"
                f"  (SELECT COUNT(*) > 0 FROM {_qi(table)})"
                ")",
                (table, col),
            )
    pconn.commit()


def reconcile(sconn: sqlite3.Connection, pconn, tables: Sequence[str]) -> Dict:
    """逐表行数对拍 + 账本按 wallet 勾稽。返回结构化报告（ok=False 表示行数不一致）。"""
    report: Dict = {"ok": True, "tables": {}, "ledger": None}
    for table in tables:
        s_n = sconn.execute(f"SELECT COUNT(*) FROM {_qi(table)}").fetchone()[0]
        with pconn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_qi(table)}")
            p_n = cur.fetchone()[0]
        match = int(s_n) == int(p_n)
        report["tables"][table] = {"sqlite": int(s_n), "pg": int(p_n), "match": match}
        if not match:
            report["ok"] = False

    if "entitlement_wallets" in tables and "entitlement_ledger" in tables:
        with pconn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.balance_shell_micros,
                       COALESCE(SUM(l.amount_shell_micros), 0) AS ledger_sum
                FROM entitlement_wallets w
                LEFT JOIN entitlement_ledger l ON l.wallet_id = w.id
                GROUP BY w.id, w.balance_shell_micros
                """
            )
            rows = cur.fetchall()
        mismatches = [
            {"wallet_id": wid, "balance": int(bal), "ledger_sum": int(lsum)}
            for (wid, bal, lsum) in rows
            if int(bal) != int(lsum)
        ]
        report["ledger"] = {
            "wallets": len(rows),
            "balanced": len(rows) - len(mismatches),
            "mismatches": mismatches,
        }
    return report


def migrate(sqlite_path: str, pg_dsn: str, *, batch: int = 500, truncate: bool = False,
            do_reconcile: bool = True) -> Dict:
    """主入口：建 PG schema → 逐表复制 → 对拍。返回 {"copied": {...}, "reconcile": {...}}。"""
    import psycopg

    from app.config import settings

    # 1) 在 PG 上建全量 schema（临时把 settings 指向 PG，建完恢复，不影响调用方进程其余逻辑）
    original_url = getattr(settings, "database_url", "")
    settings.database_url = pg_dsn
    try:
        from app.db import init_db

        init_db()
    finally:
        settings.database_url = original_url

    sconn = sqlite3.connect(sqlite_path)
    pconn = psycopg.connect(pg_dsn, autocommit=False)
    try:
        src_tables = discover_sqlite_tables(sconn)
        tgt_tables = pg_tables(pconn)
        tables = [t for t in src_tables if t in tgt_tables]
        skipped = [t for t in src_tables if t not in tgt_tables]

        copied: Dict[str, int] = {}
        for table in tables:
            copied[table] = copy_table(sconn, pconn, table, batch=batch, truncate=truncate)

        result: Dict = {"copied": copied, "skipped": skipped, "reconcile": None}
        if do_reconcile:
            result["reconcile"] = reconcile(sconn, pconn, tables)
        return result
    finally:
        sconn.close()
        pconn.close()


def _print_report(result: Dict) -> bool:
    """打印报告，返回是否一切正常（行数对拍通过且账本无勾稽差异）。"""
    print("=== 复制行数 ===")
    for table, n in sorted(result["copied"].items()):
        print(f"  {table:40s} {n}")
    if result.get("skipped"):
        print("=== 跳过（目标 schema 不含）===")
        for t in result["skipped"]:
            print(f"  {t}")
    rec = result.get("reconcile")
    ok = True
    if rec is not None:
        print("=== 行数对拍 ===")
        for table, info in sorted(rec["tables"].items()):
            flag = "OK" if info["match"] else "MISMATCH"
            if not info["match"]:
                ok = False
                print(f"  [{flag}] {table}: sqlite={info['sqlite']} pg={info['pg']}")
        if rec["ok"]:
            print("  全部表行数一致 ✓")
        ledger = rec.get("ledger")
        if ledger is not None:
            print("=== 账本勾稽（wallet balance vs SUM(ledger.amount)）===")
            print(f"  钱包数={ledger['wallets']} 勾稽通过={ledger['balanced']}")
            for m in ledger["mismatches"]:
                ok = False
                print(f"  [MISMATCH] wallet={m['wallet_id']} balance={m['balance']} ledger_sum={m['ledger_sum']}")
            if not ledger["mismatches"]:
                print("  全部钱包勾稽一致 ✓")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移 + 对拍")
    parser.add_argument("--sqlite", required=True, help="源 SQLite 文件路径")
    parser.add_argument("--pg", required=True, help="目标 PG DSN（postgresql://...）")
    parser.add_argument("--batch", type=int, default=500, help="批量插入大小（默认 500）")
    parser.add_argument("--truncate", action="store_true", help="复制前 TRUNCATE 目标表（重跑用）")
    parser.add_argument("--no-reconcile", action="store_true", help="跳过对拍")
    args = parser.parse_args()

    result = migrate(
        args.sqlite, args.pg, batch=args.batch, truncate=args.truncate,
        do_reconcile=not args.no_reconcile,
    )
    ok = _print_report(result)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
