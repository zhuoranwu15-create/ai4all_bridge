#!/usr/bin/env python3
"""Phase 1 多产品迁移前/expand 后只读预检。

脚本不调用 ``init_db``，并按目标库当前列集选择 legacy 或 contract reconcile；不会输出
手机号、消息正文、token 或数据行样本。任一 BLOCK 命中时退出码为 1。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass(frozen=True)
class CheckResult:
    """一项仅含聚合计数的预检结果。"""

    name: str
    description: str
    total: int
    severity: str = "BLOCK"


@dataclass(frozen=True)
class PrecheckReport:
    """Phase 1 迁移前报告；不承载任何生产数据样本。"""

    inventory: Dict[str, int]
    checks: List[CheckResult]

    def blocking(self) -> List[CheckResult]:
        """返回所有已触发的阻断项。"""

        return [
            check
            for check in self.checks
            if check.severity == "BLOCK" and check.total > 0
        ]


_INVENTORY_SQL = """
SELECT
  (SELECT COUNT(*) FROM platform_users) AS platform_users,
  (SELECT COUNT(*) FROM accounts) AS accounts,
  (SELECT COUNT(*) FROM account_owner_bindings WHERE status='active') AS active_bindings,
  (SELECT COUNT(*) FROM platform_user_sessions) AS sessions_total,
  (SELECT COUNT(*) FROM platform_user_sessions
     WHERE expires_at > to_char(
       (now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'
     )) AS sessions_unexpired,
  (SELECT COUNT(*) FROM subscriptions WHERE status='active') AS active_subscriptions,
  (SELECT COUNT(*) FROM entitlement_wallets WHERE status='active') AS active_wallets,
  (SELECT COUNT(*) FROM entitlement_ledger) AS ledger_entries,
  (SELECT COUNT(*) FROM daily_usage) AS daily_usage_rows,
  (SELECT COUNT(*) FROM daily_quota_reservations) AS quota_reservations,
  (SELECT COUNT(*) FROM referral_relationships) AS referral_relationships
"""


_CHECKS = (
    (
        "binding_app_drift",
        "owner binding 的 app_id 与 account.app_id 不一致或账号不存在",
        """
        SELECT b.id
        FROM account_owner_bindings b
        LEFT JOIN accounts a ON a.id = b.account_id
        WHERE a.id IS NULL OR b.app_id IS NULL OR a.app_id IS NULL OR b.app_id <> a.app_id
        """,
    ),
    (
        "unsupported_account_app",
        "m0037 回填前存在非 zhaoxi account/binding，不能安全做单产品回填",
        """
        SELECT id FROM accounts WHERE app_id IS NULL OR app_id <> 'zhaoxi'
        UNION ALL
        SELECT CAST(id AS TEXT) FROM account_owner_bindings
        WHERE app_id IS NULL OR app_id <> 'zhaoxi'
        """,
    ),
    (
        "duplicate_active_entry_account",
        "同一真人和产品存在多条 active 入口账号绑定",
        """
        SELECT platform_user_id, app_id
        FROM account_owner_bindings
        WHERE status='active'
        GROUP BY platform_user_id, app_id
        HAVING COUNT(*) > 1
        """,
    ),
    (
        "duplicate_active_subscription",
        "同一真人存在多条 active subscription，MP-02 前须确认归并规则",
        """
        SELECT platform_user_id
        FROM subscriptions
        WHERE status='active'
        GROUP BY platform_user_id
        HAVING COUNT(*) > 1
        """,
    ),
    (
        "wallet_owner_drift",
        "active wallet 的真人归属与创建来源账号的 active owner 不一致或无法解析",
        """
        SELECT w.id
        FROM entitlement_wallets w
        WHERE w.status='active'
          AND (
            (
              NOT EXISTS (
                SELECT 1 FROM account_owner_bindings b
                WHERE b.account_id=w.account_id AND b.status='active'
                  AND b.platform_user_id=w.platform_user_id
              )
              AND NOT EXISTS (
                SELECT 1 FROM universe_residents r
                JOIN universes u ON u.id=r.universe_id
                WHERE r.runtime_account_id=w.account_id
                  AND u.owner_platform_user_id=w.platform_user_id
              )
            )
            OR EXISTS (
              SELECT 1 FROM account_owner_bindings b
              WHERE b.account_id=w.account_id AND b.status='active'
                AND b.platform_user_id<>w.platform_user_id
            )
            OR EXISTS (
              SELECT 1 FROM universe_residents r
              JOIN universes u ON u.id=r.universe_id
              WHERE r.runtime_account_id=w.account_id
                AND u.owner_platform_user_id<>w.platform_user_id
            )
          )
        """,
    ),
    (
        "ledger_owner_drift",
        "ledger 的 wallet/account/真人引用缺失或与 wallet 真人归属不一致",
        """
        SELECT l.id
        FROM entitlement_ledger l
        LEFT JOIN entitlement_wallets w ON w.id=l.wallet_id
        LEFT JOIN accounts a ON a.id=l.account_id
        LEFT JOIN platform_users pu ON pu.id=l.platform_user_id
        WHERE w.id IS NULL OR a.id IS NULL OR pu.id IS NULL
           OR l.platform_user_id<>w.platform_user_id
        """,
    ),
    (
        "wallet_ledger_balance_mismatch",
        "wallet 当前余额与其 ledger amount 合计不相等",
        """
        SELECT w.id
        FROM entitlement_wallets w
        LEFT JOIN entitlement_ledger l ON l.wallet_id=w.id
        GROUP BY w.id, w.balance_shell_micros
        HAVING w.balance_shell_micros <> COALESCE(SUM(l.amount_shell_micros), 0)
        """,
    ),
    (
        "quota_owner_drift",
        "daily usage/reservation 的真人 subject 与入口账号 owner 不一致",
        """
        SELECT CAST(d.id AS TEXT)
        FROM daily_usage d
        JOIN account_owner_bindings b
          ON b.account_id=d.account_id AND b.status='active'
        WHERE d.platform_user_id<>b.platform_user_id
        UNION ALL
        SELECT q.id
        FROM daily_quota_reservations q
        JOIN account_owner_bindings b
          ON b.account_id=q.account_id AND b.status='active'
        WHERE q.platform_user_id<>b.platform_user_id
        UNION ALL
        SELECT CAST(d.id AS TEXT)
        FROM daily_usage d
        JOIN universe_residents r ON r.runtime_account_id=d.account_id
        JOIN universes u ON u.id=r.universe_id
        WHERE d.platform_user_id<>u.owner_platform_user_id
        UNION ALL
        SELECT q.id
        FROM daily_quota_reservations q
        JOIN universe_residents r ON r.runtime_account_id=q.account_id
        JOIN universes u ON u.id=r.universe_id
        WHERE q.platform_user_id<>u.owner_platform_user_id
        """,
    ),
    (
        "quota_owner_fallback",
        "daily usage/reservation 的 subject 不是现存 platform_user（仍为 account fallback）",
        """
        SELECT CAST(d.id AS TEXT)
        FROM daily_usage d
        LEFT JOIN platform_users pu ON pu.id=d.platform_user_id
        WHERE pu.id IS NULL
        UNION ALL
        SELECT q.id
        FROM daily_quota_reservations q
        LEFT JOIN platform_users pu ON pu.id=q.platform_user_id
        WHERE pu.id IS NULL
        """,
    ),
    (
        "referral_orphan_reference",
        "referral relationship/review 存在缺失的用户、邀请码、关系或账号引用",
        """
        SELECT rr.id
        FROM referral_relationships rr
        LEFT JOIN platform_users inviter ON inviter.id=rr.inviter_platform_user_id
        LEFT JOIN platform_users invitee ON invitee.id=rr.invitee_platform_user_id
        LEFT JOIN referral_codes rc ON rc.id=rr.referral_code_id
        WHERE inviter.id IS NULL OR invitee.id IS NULL OR rc.id IS NULL
        UNION ALL
        SELECT mr.id
        FROM meaningful_message_reviews mr
        LEFT JOIN referral_relationships rr ON rr.id=mr.referral_relationship_id
        LEFT JOIN platform_users invitee ON invitee.id=mr.invitee_platform_user_id
        LEFT JOIN accounts a ON a.id=mr.account_id
        WHERE rr.id IS NULL OR invitee.id IS NULL OR a.id IS NULL
        """,
    ),
)

# 这些存量只影响后续工单的 contract，不会被 m0037/m0038 读取或改写。
# 仍持续盘点并醒目标 WARN，但不阻断 MP-01 的加性迁移。
_DEFERRED_WARN_CHECKS = {"quota_owner_fallback"}

_BILLING_CONTRACT_DESCRIPTIONS = {
    "null_app_id": "计费子表仍有空 app_id",
    "duplicate_active_subscription": "同一真人和产品存在多条 active subscription",
    "duplicate_active_wallet": "同一真人和产品存在多个 active wallet",
    "subscription_membership_drift": "subscription 找不到同产品 membership",
    "wallet_scope_drift": "wallet 与创建来源 account/membership 的产品归属不一致",
    "ledger_scope_drift": "ledger 与 wallet/account/membership 的产品归属不一致",
    "cost_scope_drift": "cost 与 account/wallet/ledger 的产品归属不一致",
    "wallet_ledger_balance_mismatch": "wallet 当前余额与同产品 ledger amount 合计不相等",
    "duplicate_ledger_app_idempotency": "同一产品存在重复 ledger 幂等键",
    "duplicate_cost_app_idempotency": "同一产品存在重复 cost event 幂等键",
}

_QUOTA_CONTRACT_DESCRIPTIONS = {
    "quota_null_app_id": "daily usage/reservation 仍有空 app_id",
    "quota_owner_fallback": "daily usage/reservation 仍使用非真人 account fallback subject",
    "daily_scope_drift": "daily usage 与 account/owner/membership 产品归属不一致",
    "reservation_scope_drift": "quota reservation 与 account/owner/membership 产品归属不一致",
    "duplicate_daily_usage": "同一真人、产品和日期存在多条 daily usage",
}

_REFERRAL_CONTRACT_DESCRIPTIONS = {
    "referral_null_app_id": "邀请码、邀请关系或有效消息审核仍有空 app_id",
    "duplicate_personal_code": "同一真人和产品存在多个个人邀请码",
    "duplicate_invitee_membership": "同一真人和产品存在多条邀请关系",
    "referral_code_scope_drift": "个人邀请码找不到同产品 membership",
    "referral_relationship_scope_drift": "邀请关系与 code/membership/reward ledger 产品归属不一致",
    "meaningful_review_scope_drift": "有效消息审核与 relationship/account 产品归属不一致",
}

_IDENTITY_CONTRACT_DESCRIPTIONS = {
    "identity_null_app_id": "account、owner binding、session 或 membership 仍有空 app_id",
    "binding_app_drift": "owner binding 与 account/user/membership 的产品归属不一致",
    "session_scope_drift": "session 找不到对应真人或同产品 membership",
    "resident_scope_drift": "resident runtime account 找不到同产品 owner membership",
    "message_account_drift": "message 的 account 与所属 runtime session 不一致",
    "duplicate_active_entry_account": "同一真人和产品存在多条 active 入口账号绑定",
    "duplicate_active_account_owner": "同一入口账号同时存在多个 active owner",
}

_PHASE1_CONTRACT_DESCRIPTIONS = {
    **_IDENTITY_CONTRACT_DESCRIPTIONS,
    **_BILLING_CONTRACT_DESCRIPTIONS,
    **_QUOTA_CONTRACT_DESCRIPTIONS,
    **_REFERRAL_CONTRACT_DESCRIPTIONS,
}


def _count(conn, sql: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM ({sql}) precheck_rows").fetchone()
    return int(row["n"])


def run_precheck(conn) -> PrecheckReport:
    """在调用方连接上执行全部 SELECT，并返回不含明细的结构化报告。"""

    from app.db import _core

    inventory_row = conn.execute(_INVENTORY_SQL).fetchone()
    inventory = {key: int(inventory_row[key]) for key in inventory_row.keys()}
    identity_expanded = _identity_contract_columns_present(conn)
    billing_expanded = _billing_app_columns_present(conn)
    quota_expanded = _quota_app_columns_present(conn)
    referral_expanded = _referral_app_columns_present(conn)
    fully_expanded = (
        identity_expanded
        and billing_expanded
        and quota_expanded
        and referral_expanded
    )
    if fully_expanded:
        counts = _core._phase1_contract_violation_counts(conn)
        return PrecheckReport(
            inventory=inventory,
            checks=[
                CheckResult(
                    name=name,
                    description=_PHASE1_CONTRACT_DESCRIPTIONS[name],
                    total=total,
                )
                for name, total in counts.items()
            ],
        )
    # expand 前继续使用 legacy 检查；expand 后由 m0040 同源 reconcile 按 app 分组，
    # 避免把两个产品各一条 active 错报成重复。
    replaced_after_expand = {
        "unsupported_account_app",
        "duplicate_active_subscription",
        "wallet_ledger_balance_mismatch",
    }
    replaced_after_identity_expand = {
        "binding_app_drift",
        "duplicate_active_entry_account",
    }
    checks = []
    for name, description, sql in _CHECKS:
        if identity_expanded and name in replaced_after_identity_expand:
            continue
        if billing_expanded and name in replaced_after_expand:
            continue
        if quota_expanded and name in {"quota_owner_drift", "quota_owner_fallback"}:
            continue
        if referral_expanded and name == "referral_orphan_reference":
            continue
        checks.append(
            CheckResult(
                name=name,
                description=description,
                total=_count(conn, sql),
                severity=(
                    "WARN"
                    if name in _DEFERRED_WARN_CHECKS and not quota_expanded
                    else "BLOCK"
                ),
            )
        )
    if identity_expanded:
        counts = _core._identity_contract_violation_counts(conn)
        checks.extend(
            CheckResult(
                name=name,
                description=_IDENTITY_CONTRACT_DESCRIPTIONS[name],
                total=total,
            )
            for name, total in counts.items()
        )
    if billing_expanded:
        counts = _core._billing_contract_violation_counts(conn)
        checks.extend(
            CheckResult(
                name=name,
                description=_BILLING_CONTRACT_DESCRIPTIONS[name],
                total=total,
            )
            for name, total in counts.items()
        )
    if quota_expanded:
        counts = _core._quota_contract_violation_counts(conn)
        checks.extend(
            CheckResult(
                name=name,
                description=_QUOTA_CONTRACT_DESCRIPTIONS[name],
                total=total,
            )
            for name, total in counts.items()
        )
    if referral_expanded:
        counts = _core._referral_contract_violation_counts(conn)
        checks.extend(
            CheckResult(
                name=name,
                description=_REFERRAL_CONTRACT_DESCRIPTIONS[name],
                total=total,
            )
            for name, total in counts.items()
        )
    return PrecheckReport(inventory=inventory, checks=checks)


def _identity_contract_columns_present(conn) -> bool:
    """判断 m0037/m0038 身份与 session 产品列是否已就绪。"""
    rows = conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND ((table_name='product_memberships' AND column_name='app_id')
            OR (table_name='platform_user_sessions' AND column_name='app_id'))
        """
    ).fetchall()
    return {
        (str(row["table_name"]), str(row["column_name"])) for row in rows
    } == {
        ("product_memberships", "app_id"),
        ("platform_user_sessions", "app_id"),
    }


def _billing_app_columns_present(conn) -> bool:
    """判断 m0039 是否已展开；expand 前也可安全运行同一只读脚本。"""
    tables = (
        "subscriptions",
        "entitlement_wallets",
        "entitlement_ledger",
        "cost_events",
    )
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND column_name='app_id'
          AND table_name IN ('subscriptions', 'entitlement_wallets',
                             'entitlement_ledger', 'cost_events')
        """
    ).fetchall()
    return {str(row["table_name"]) for row in rows} == set(tables)


def _quota_app_columns_present(conn) -> bool:
    """判断 m0041 是否已展开；expand 前不得引用尚不存在的 quota app_id。"""
    tables = ("daily_usage", "daily_quota_reservations")
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND column_name='app_id'
          AND table_name IN ('daily_usage', 'daily_quota_reservations')
        """
    ).fetchall()
    return {str(row["table_name"]) for row in rows} == set(tables)


def _referral_app_columns_present(conn) -> bool:
    """判断 m0043 是否已展开；expand 前不得引用 referral app_id。"""
    tables = (
        "referral_codes",
        "referral_relationships",
        "meaningful_message_reviews",
    )
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND column_name='app_id'
          AND table_name IN ('referral_codes', 'referral_relationships',
                             'meaningful_message_reviews')
        """
    ).fetchall()
    return {str(row["table_name"]) for row in rows} == set(tables)


def _print_report(report: PrecheckReport) -> None:
    print("=== Multi-product Phase 1 precheck ===")
    print("backend=postgresql")
    print("inventory:")
    for name, total in report.inventory.items():
        print(f"  {name}={total}")
    print("checks:")
    for check in report.checks:
        state = "PASS" if check.total == 0 else check.severity
        print(f"  [{state}] {check.name}={check.total}: {check.description}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg", default=None, help="目标 PG DSN；不传则读取当前 DATABASE_URL")
    args = parser.parse_args()
    if args.pg:
        from app.config import settings

        settings.database_url = args.pg

    from app.db import _core

    with _core.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        report = run_precheck(conn)

    _print_report(report)
    blocking = report.blocking()
    print("result=BLOCK" if blocking else "result=PASS")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
