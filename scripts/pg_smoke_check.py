#!/usr/bin/env python3
"""PG 冒烟验证（只读）：把 settings.database_url 指向 PG，跑生产读路径，并与源 SQLite 对拍。

不写任何数据、不改 .env、不重启服务。用于迁移后确认 PG 库 schema 与数据可用、账号隔离正确。

用法：
    .venv/bin/python scripts/pg_smoke_check.py \
        --pg "postgresql://ai4all:120502PG@localhost:5432/ai4all" \
        --sqlite data/ai4all.sqlite3 \
        --account aid_806382741
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg", required=True)
    ap.add_argument("--sqlite", default="data/ai4all.sqlite3")
    ap.add_argument("--account", default="aid_806382741")
    args = ap.parse_args()

    from app.config import settings
    settings.database_url = args.pg  # 仅本进程内生效

    from app.db import _core
    from app.db import accounts, billing

    ok = True

    # 0) 后端确认
    print("=== 后端 ===")
    print(f"  is_postgres = {_core.is_postgres()}")
    if not _core.is_postgres():
        print("  [FAIL] settings 未指向 PG")
        return 1

    # 1) json_patch 函数存在（厚节点 schema 依赖）+ JSON 列可读
    print("=== schema 自检 ===")
    with _core.connect() as conn:
        row = conn.execute(
            "SELECT to_regprocedure('json_patch(jsonb,jsonb)') IS NOT NULL AS has_fn"
        ).fetchone()
        has_fn = bool(row["has_fn"]) if row else False
        print(f"  json_patch 函数存在 = {has_fn}")
        ok = ok and has_fn
        n_tables = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        ).fetchone()["n"]
        print(f"  public 表数量 = {n_tables}")

    # 2) 真实读路径（生产函数）
    print(f"=== 账号读路径: {args.account} ===")
    acct = accounts.get_account(account_id=args.account)
    print(f"  get_account -> {'命中' if acct else 'None'}")
    ok = ok and acct is not None

    sessions = accounts.list_sessions_for_account(account_id=args.account, limit=50)
    print(f"  list_sessions_for_account -> {len(sessions)} 条")

    msgs = accounts.list_recent_messages_for_account(account_id=args.account, limit=20)
    print(f"  list_recent_messages_for_account -> {len(msgs)} 条")
    if msgs:
        last = msgs[-1]
        preview = str(last.get("content", ""))[:40].replace("\n", " ")
        print(f"    最近一条 role={last.get('role')} content[:40]={preview!r}")

    wallet = billing.get_wallet_summary(account_id=args.account, create_if_missing=False)
    if wallet:
        # get_wallet_summary 返回嵌套结构：{'wallet': {...}, 'latest_ledger': ..., 'display': ...}
        w = wallet.get("wallet") or {}
        print(f"  get_wallet_summary -> balance_shell_micros={w.get('balance_shell_micros')}")
    else:
        print("  get_wallet_summary -> None（该账号可能无 owner 绑定）")

    # 3) 账号隔离 + 数据完整性对拍：同账号在 SQLite 与 PG 的关键计数应一致
    print("=== SQLite ↔ PG 同账号对拍 ===")
    sconn = sqlite3.connect(args.sqlite)
    sconn.row_factory = sqlite3.Row

    def s_count(sql, params):
        return sconn.execute(sql, params).fetchone()[0]

    def p_count(sql, params):
        with _core.connect() as conn:
            return conn.execute(sql, params).fetchone()[0]

    checks = [
        ("accounts(本账号)",
         "SELECT count(*) FROM accounts WHERE id=?",
         "SELECT count(*) FROM accounts WHERE id=?"),
        ("sessions(本账号)",
         "SELECT count(*) FROM sessions WHERE account_id=?",
         "SELECT count(*) FROM sessions WHERE account_id=?"),
        ("messages(本账号)",
         "SELECT count(*) FROM messages WHERE account_id=?",
         "SELECT count(*) FROM messages WHERE account_id=?"),
    ]
    for label, ssql, psql in checks:
        sn = s_count(ssql, (args.account,))
        pn = p_count(psql, (args.account,))
        flag = "OK" if sn == pn else "MISMATCH"
        if sn != pn:
            ok = False
        print(f"  [{flag}] {label}: sqlite={sn} pg={pn}")

    # 4) 隔离反向校验：PG 里本账号 messages 全部 account_id 一致（无串号）
    with _core.connect() as conn:
        bad = conn.execute(
            "SELECT count(*) AS n FROM messages WHERE account_id <> ? "
            "AND session_id IN (SELECT id FROM sessions WHERE account_id=?)",
            (args.account, args.account),
        ).fetchone()["n"]
    flag = "OK" if bad == 0 else "MISMATCH"
    if bad:
        ok = False
    print(f"  [{flag}] 隔离反查: 本账号 session 下挂着他账号 message = {bad}")

    sconn.close()
    print("=== 结论 ===")
    print("  全部通过 ✓" if ok else "  存在问题 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
