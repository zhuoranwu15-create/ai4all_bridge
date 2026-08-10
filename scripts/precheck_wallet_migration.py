#!/usr/bin/env python3
"""D-14 钱包唯一键上迁（account_id → platform_user_id）生产预检（只读）。

M1-0 前置门：M1-1 迁移把 `entitlement_wallets` 唯一键从 `account_id` 上迁到
`platform_user_id`（一真人一钱包），并对多钱包老用户做「余额求和 + ledger/cost_events
归并到选主钱包（选主 = 最早 active binding 对应钱包）+ 其余钱包置 status='merged'」。
本脚本在跑迁移前盘点生产 PG，产出多钱包用户清单，并校验迁移所依赖的四条数据不变量。
不写任何数据、不改 .env、不调 init_db（避免误跑迁移）。

背景见 ADR D-14（docs/architecture/products/mingchan/companion_world_3_0_refactor_design.md §D-14）。
发布判定必须在目标 PostgreSQL 上运行。

阻断发布条件（任一 total>0 即 BLOCK，禁止执行 M1-1 迁移，须先人工修数）：
  1. ambiguous_owner   —— 单个 account 有 >1 条 active owner binding，归属歧义，
                          迁移无法确定性地把钱包归到某个真人。
  2. orphan_wallet     —— active 钱包对应的 account 没有任何 active binding，
                          无法经 binding 归属到活跃真人。
  3. owner_drift       —— 钱包 platform_user_id 与其 account 的唯一 active 归属人不一致；
                          迁移按 wallet.platform_user_id 分组，drift 会把余额并到错误的人。
  4. primary_undefined —— 多钱包用户「最早 active binding 对应 account」没有 active 钱包，
                          冻结的选主规则取不到主钱包，须先定 fallback 再迁。
WARN（不自动阻断，但须人工过目）：负余额钱包、非 PG 运行（结果不作发布依据）。

用法：
    # 生产 PG（发布闸；退出码 0=PASS 可迁移，1=BLOCK 禁止迁移）
    .venv/bin/python scripts/precheck_wallet_migration.py \
        --pg "postgresql://ai4all:***@localhost:5432/ai4all"

    # 不传 --pg 则使用当前 .env 的 PostgreSQL DATABASE_URL
    .venv/bin/python scripts/precheck_wallet_migration.py
"""
import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --- 盘点聚合（informational） ---------------------------------------------

_SQL_TOTALS = """
SELECT
  (SELECT COUNT(*) FROM entitlement_wallets WHERE status='active') AS active_wallets,
  (SELECT COUNT(DISTINCT platform_user_id) FROM entitlement_wallets WHERE status='active')
    AS distinct_owners
"""

_SQL_MULTI_WALLET_USERS = """
SELECT w.platform_user_id AS platform_user_id,
       COUNT(*) AS wallet_count,
       SUM(w.balance_shell_micros) AS total_balance,
       SUM(CASE WHEN w.balance_shell_micros <> 0 THEN 1 ELSE 0 END) AS nonzero_wallets
FROM entitlement_wallets w
WHERE w.status='active'
GROUP BY w.platform_user_id
HAVING COUNT(*) > 1
ORDER BY wallet_count DESC, total_balance DESC
"""

# 已被多次赠权的真人（每个多出来的 account 各赠一次）——为 M1-2/M1-7 归并去重定量。
_SQL_MULTI_GRANT_USERS = """
SELECT platform_user_id AS platform_user_id,
       COUNT(*) AS grant_count,
       SUM(amount_shell_micros) AS granted_total
FROM entitlement_ledger
WHERE source_type='new_user_grant'
GROUP BY platform_user_id
HAVING COUNT(*) > 1
ORDER BY grant_count DESC
"""

# --- 阻断条件（BLOCK） ------------------------------------------------------

_SQL_AMBIGUOUS_OWNER = """
SELECT account_id AS account_id, COUNT(*) AS active_bindings
FROM account_owner_bindings
WHERE status='active'
GROUP BY account_id
HAVING COUNT(*) > 1
"""

_SQL_ORPHAN_WALLET = """
SELECT w.id AS wallet_id, w.account_id AS account_id,
       w.platform_user_id AS platform_user_id, w.balance_shell_micros AS balance_shell_micros
FROM entitlement_wallets w
WHERE w.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM account_owner_bindings b
    WHERE b.account_id = w.account_id AND b.status='active'
  )
"""

# 仅在「恰好一个 active 归属人」时判 drift；歧义/孤儿由上面两条各自捕获，不重复计。
_SQL_OWNER_DRIFT = """
SELECT w.id AS wallet_id, w.account_id AS account_id,
       w.platform_user_id AS wallet_owner, b.platform_user_id AS binding_owner
FROM entitlement_wallets w
JOIN account_owner_bindings b
  ON b.account_id = w.account_id AND b.status='active'
WHERE w.status='active'
  AND w.platform_user_id <> b.platform_user_id
  AND (SELECT COUNT(*) FROM account_owner_bindings b2
       WHERE b2.account_id = w.account_id AND b2.status='active') = 1
"""

# 多钱包用户中，「最早 active binding 对应 account」没有 active 钱包者（选主取不到主）。
_SQL_PRIMARY_UNDEFINED = """
SELECT m.platform_user_id AS platform_user_id
FROM (
    SELECT platform_user_id
    FROM entitlement_wallets
    WHERE status='active'
    GROUP BY platform_user_id
    HAVING COUNT(*) > 1
) m
WHERE NOT EXISTS (
    SELECT 1
    FROM account_owner_bindings b
    WHERE b.status='active'
      AND b.platform_user_id = m.platform_user_id
      AND NOT EXISTS (
          SELECT 1 FROM account_owner_bindings b2
          WHERE b2.status='active'
            AND b2.platform_user_id = b.platform_user_id
            AND (b2.created_at < b.created_at
                 OR (b2.created_at = b.created_at AND b2.id < b.id))
      )
      AND EXISTS (
          SELECT 1 FROM entitlement_wallets w
          WHERE w.status='active' AND w.account_id = b.account_id
      )
)
"""

# WARN
_SQL_NEGATIVE_BALANCE = """
SELECT id AS wallet_id, account_id AS account_id,
       platform_user_id AS platform_user_id, balance_shell_micros AS balance_shell_micros
FROM entitlement_wallets
WHERE status='active' AND balance_shell_micros < 0
"""

# (name, kind, columns, sql, description)
_BLOCK_CHECKS = [
    ("ambiguous_owner", "BLOCK", ["account_id", "active_bindings"],
     _SQL_AMBIGUOUS_OWNER, "account 有 >1 条 active owner binding（归属歧义）"),
    ("orphan_wallet", "BLOCK", ["wallet_id", "account_id", "platform_user_id", "balance_shell_micros"],
     _SQL_ORPHAN_WALLET, "active 钱包的 account 无 active binding（无法归属真人）"),
    ("owner_drift", "BLOCK", ["wallet_id", "account_id", "wallet_owner", "binding_owner"],
     _SQL_OWNER_DRIFT, "钱包 platform_user_id 与唯一 active 归属人不一致（会并错人）"),
    ("primary_undefined", "BLOCK", ["platform_user_id"],
     _SQL_PRIMARY_UNDEFINED, "多钱包用户最早 active binding 对应 account 无 active 钱包（选主取不到）"),
    ("negative_balance", "WARN", ["wallet_id", "account_id", "platform_user_id", "balance_shell_micros"],
     _SQL_NEGATIVE_BALANCE, "active 钱包负余额（人工过目）"),
]


@dataclass
class CheckResult:
    """一条异常检查的结果：total 为全量命中数，rows 为截断样本。"""
    name: str
    kind: str  # "BLOCK" | "WARN"
    description: str
    total: int
    rows: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PrecheckReport:
    """整个预检的结构化结果（可单测；main() 只负责打印与退出码）。"""
    active_wallets: int
    distinct_owners: int
    multi_wallet_users: List[Dict[str, Any]]
    multi_grant_users: List[Dict[str, Any]]
    checks: List[CheckResult]

    def blocking(self) -> List[CheckResult]:
        """触发的硬阻断条件（BLOCK 且 total>0）——非空即禁止执行 M1-1 迁移。"""
        return [c for c in self.checks if c.kind == "BLOCK" and c.total > 0]


def _count_and_sample(
    conn, base_sql: str, columns: List[str], sample_limit: int
) -> tuple:
    """对一段 SELECT 返回 (全量命中数, 截断样本 rows)。只读，后端中立。"""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM ({base_sql}) _sub"
    ).fetchone()["n"]
    sample = [
        {col: row[col] for col in columns}
        for row in conn.execute(f"{base_sql} LIMIT {int(sample_limit)}").fetchall()
    ]
    return int(total), sample


def run_precheck(conn, *, sample_limit: int = 50) -> PrecheckReport:
    """在给定连接上跑全部只读预检查询，返回结构化 PrecheckReport。

    conn 由调用方用 `app.db._core.connect()` 提供（生产为 PG）。本函数不写任何数据。
    """
    totals = conn.execute(_SQL_TOTALS).fetchone()
    _, multi_wallet = _count_and_sample(
        conn, _SQL_MULTI_WALLET_USERS,
        ["platform_user_id", "wallet_count", "total_balance", "nonzero_wallets"],
        sample_limit,
    )
    _, multi_grant = _count_and_sample(
        conn, _SQL_MULTI_GRANT_USERS,
        ["platform_user_id", "grant_count", "granted_total"],
        sample_limit,
    )

    checks: List[CheckResult] = []
    for name, kind, columns, sql, description in _BLOCK_CHECKS:
        total, rows = _count_and_sample(conn, sql, columns, sample_limit)
        checks.append(
            CheckResult(name=name, kind=kind, description=description, total=total, rows=rows)
        )

    return PrecheckReport(
        active_wallets=int(totals["active_wallets"]),
        distinct_owners=int(totals["distinct_owners"]),
        multi_wallet_users=multi_wallet,
        multi_grant_users=multi_grant,
        checks=checks,
    )


def _print_report(report: PrecheckReport) -> None:
    print("=== 后端 ===")
    print("  postgresql")

    print("=== 盘点 ===")
    print(f"  active 钱包总数 = {report.active_wallets}")
    print(f"  distinct 归属真人数 = {report.distinct_owners}")
    print(f"  多钱包用户数（需合并）= {len(report.multi_wallet_users)}")
    for row in report.multi_wallet_users[:20]:
        print(
            f"    user={row['platform_user_id']} 钱包数={row['wallet_count']} "
            f"合计余额={row['total_balance']} 非零钱包={row['nonzero_wallets']}"
        )
    print(f"  多次赠权用户数（M1-2/M1-7 去重面）= {len(report.multi_grant_users)}")
    for row in report.multi_grant_users[:20]:
        print(
            f"    user={row['platform_user_id']} 赠权次数={row['grant_count']} "
            f"累计赠额={row['granted_total']}"
        )

    print("=== 检查 ===")
    for check in report.checks:
        flag = "OK" if check.total == 0 else check.kind
        print(f"  [{flag}] {check.name}: {check.total} —— {check.description}")
        for row in check.rows[:10]:
            print(f"      {row}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pg", default=None, help="PG DSN；不传则用当前 DATABASE_URL")
    ap.add_argument("--sample-limit", type=int, default=50, help="每条检查打印的样本上限")
    args = ap.parse_args()

    if args.pg:
        from app.config import settings
        settings.database_url = args.pg  # 仅本进程内生效，不改 .env

    from app.db import _core

    with _core.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        report = run_precheck(conn, sample_limit=args.sample_limit)

    _print_report(report)

    blocking = report.blocking()
    print("=== 结论 ===")
    if blocking:
        names = ", ".join(f"{c.name}({c.total})" for c in blocking)
        print(f"  BLOCK：禁止执行 M1-1 迁移，先修数 → {names}")
        return 1
    print("  PASS：无阻断条件，可执行 M1-1 迁移（仍以生产 PG 结果为准）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
