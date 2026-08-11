#!/usr/bin/env python3
"""Plum 数据模型迁移 72–75 的只读预检。

脚本不会调用 ``init_db``，不会修改目标库，也不会输出用户、会话或 Runtime 明细。
任一 BLOCK 项非零时退出码为 1。
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
    """一项只包含聚合计数的 Plum 迁移检查。"""

    name: str
    description: str
    total: int
    severity: str = "BLOCK"


@dataclass(frozen=True)
class PrecheckReport:
    """不包含生产数据明细的 Plum 迁移预检报告。"""

    inventory: Dict[str, int]
    checks: List[CheckResult]

    def blocking(self) -> List[CheckResult]:
        """返回所有已触发的阻断项。"""

        return [check for check in self.checks if check.total > 0]


def _table_exists(conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(?) AS name", (table,)).fetchone()
    return row is not None and row["name"] is not None


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=? AND column_name=?
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _count(conn, sql: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM ({sql}) plum_precheck").fetchone()
    return int(row["n"] or 0)


def _append(
    checks: List[CheckResult], conn, name: str, description: str, sql: str
) -> None:
    checks.append(CheckResult(name=name, description=description, total=_count(conn, sql)))


def run_precheck(conn) -> PrecheckReport:
    """按目标库当前 schema 执行迁移前或迁移后的只读一致性检查。"""

    inventory = {}
    for table in (
        "plum_works",
        "plum_characters",
        "plum_user_character_relationships",
        "plum_character_bindings",
        "plum_conversations",
        "plum_connections",
        "plum_storylines",
    ):
        if _table_exists(conn, table):
            inventory[table] = _count(conn, f"SELECT 1 FROM {table}")

    checks: List[CheckResult] = []
    if _table_exists(conn, "plum_characters"):
        if _column_exists(conn, "plum_characters", "platform_effective_rating"):
            rating_sql = """
                SELECT id FROM plum_characters
                WHERE content_rating IS NULL OR BTRIM(content_rating)=''
                   OR creator_declared_rating IS NULL
                   OR BTRIM(creator_declared_rating)=''
                   OR platform_effective_rating IS NULL
                   OR BTRIM(platform_effective_rating)=''
                   OR content_rating<>platform_effective_rating
                   OR access_policy_version IS NULL
                   OR BTRIM(access_policy_version)=''
            """
        else:
            rating_sql = """
                SELECT id FROM plum_characters
                WHERE content_rating IS NULL OR BTRIM(content_rating)=''
            """
        _append(
            checks,
            conn,
            "invalid_character_rating_projection",
            "Character 分级为空或当前投影不一致，m0072 约束将失败",
            rating_sql,
        )

    if _table_exists(conn, "plum_works") and _column_exists(
        conn, "plum_works", "owner_kind"
    ):
        _append(
            checks,
            conn,
            "invalid_work_owner_scope",
            "Work owner_kind 与真人 owner 组合不合法",
            """
            SELECT id FROM plum_works
            WHERE owner_kind IS NULL OR NOT (
                (owner_kind='platform_user'
                 AND owner_platform_user_id IS NOT NULL)
                OR (owner_kind='system'
                    AND owner_platform_user_id IS NULL)
            )
            """,
        )
        _append(
            checks,
            conn,
            "legacy_fake_system_user",
            "显式 system Work 已启用，但假 Platform User 仍然存在",
            """
            SELECT id FROM platform_users WHERE id='pusr_plum_system'
            """,
        )

    if _table_exists(conn, "plum_user_character_relationships"):
        _append(
            checks,
            conn,
            "orphan_legacy_relationship",
            "旧关系缺少 Platform User 或 Character",
            """
            SELECT rel.platform_user_id
            FROM plum_user_character_relationships rel
            LEFT JOIN platform_users pu ON pu.id=rel.platform_user_id
            LEFT JOIN plum_characters ch ON ch.id=rel.character_id
            WHERE pu.id IS NULL OR ch.id IS NULL
            """,
        )

    if _table_exists(conn, "plum_character_bindings"):
        _append(
            checks,
            conn,
            "legacy_binding_runtime_scope_drift",
            "旧 Binding 的 Runtime 缺失、产品不符或已有 owner 与 User 不一致",
            """
            SELECT b.runtime_account_id
            FROM plum_character_bindings b
            LEFT JOIN platform_users pu ON pu.id=b.platform_user_id
            LEFT JOIN plum_characters ch ON ch.id=b.character_id
            LEFT JOIN accounts a ON a.id=b.runtime_account_id
            LEFT JOIN runtime_ownerships ro
              ON ro.runtime_account_id=b.runtime_account_id
            WHERE b.status='active'
              AND (pu.id IS NULL OR ch.id IS NULL OR a.id IS NULL
                   OR a.app_id<>'plum'
                   OR (ro.runtime_account_id IS NOT NULL
                       AND (ro.platform_user_id<>b.platform_user_id
                            OR ro.app_id<>'plum')))
            """,
        )

    if _table_exists(conn, "plum_conversations"):
        _append(
            checks,
            conn,
            "active_conversation_runtime_scope_drift",
            "active Conversation 的 Runtime 缺失、产品不符或已有 owner 与 User 不一致",
            """
            SELECT c.id
            FROM plum_conversations c
            LEFT JOIN accounts a ON a.id=c.runtime_account_id
            LEFT JOIN runtime_ownerships ro
              ON ro.runtime_account_id=c.runtime_account_id
            WHERE c.status='active'
              AND (a.id IS NULL OR a.app_id<>'plum'
                   OR (ro.runtime_account_id IS NOT NULL
                       AND (ro.platform_user_id<>c.platform_user_id
                            OR ro.app_id<>'plum')))
            """,
        )

    if _table_exists(conn, "plum_connection_runtime_bindings"):
        _append(
            checks,
            conn,
            "connection_runtime_owner_drift",
            "Connection Runtime Binding 找不到同 User、同产品的 active Ownership",
            """
            SELECT rb.connection_id
            FROM plum_connection_runtime_bindings rb
            LEFT JOIN runtime_ownerships ro
              ON ro.runtime_account_id=rb.runtime_account_id
             AND ro.platform_user_id=rb.platform_user_id
             AND ro.app_id='plum' AND ro.status='active'
            WHERE ro.runtime_account_id IS NULL
            """,
        )
        _append(
            checks,
            conn,
            "unbound_active_conversation",
            "active Conversation 没有匹配同 Runtime 的 Connection Binding",
            """
            SELECT c.id
            FROM plum_conversations c
            LEFT JOIN plum_connection_runtime_bindings rb
              ON rb.connection_id=c.connection_id
             AND rb.runtime_account_id=c.runtime_account_id
            WHERE c.status='active' AND rb.connection_id IS NULL
            """,
        )

    if _table_exists(conn, "plum_storylines"):
        _append(
            checks,
            conn,
            "connection_without_storyline",
            "Connection 尚未建立 Storyline",
            """
            SELECT pc.id FROM plum_connections pc
            LEFT JOIN plum_storylines ps ON ps.connection_id=pc.id
            WHERE ps.id IS NULL
            """,
        )
        _append(
            checks,
            conn,
            "storyline_without_state",
            "Storyline 尚未建立 Storyline State",
            """
            SELECT ps.id FROM plum_storylines ps
            LEFT JOIN plum_storyline_state st ON st.storyline_id=ps.id
            WHERE st.storyline_id IS NULL
            """,
        )
        _append(
            checks,
            conn,
            "conversation_storyline_scope_drift",
            "Conversation 的 Storyline 缺失或 User/Connection/Character 不一致",
            """
            SELECT c.id FROM plum_conversations c
            LEFT JOIN plum_storylines ps ON ps.id=c.storyline_id
            WHERE ps.id IS NULL
               OR ps.platform_user_id<>c.platform_user_id
               OR ps.connection_id<>c.connection_id
               OR ps.character_id<>c.character_id
            """,
        )

    return PrecheckReport(inventory=inventory, checks=checks)


def _print_report(report: PrecheckReport) -> None:
    print("=== Plum data model precheck ===")
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
    parser.add_argument("--pg", default=None, help="目标 PG DSN；不传则读取 DATABASE_URL")
    args = parser.parse_args()
    if args.pg:
        from app.config import settings

        settings.database_url = args.pg

    from app.db import _core

    with _core.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        report = run_precheck(conn)

    _print_report(report)
    print("result=BLOCK" if report.blocking() else "result=PASS")
    return 1 if report.blocking() else 0


if __name__ == "__main__":
    raise SystemExit(main())
