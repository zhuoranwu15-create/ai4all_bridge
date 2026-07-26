#!/usr/bin/env python3
"""把存量 active owner bindings 加性映射为 legacy residents（可断点、可 dry-run）。

**已不再用于常规上线（2026-07-26，产品决策 D-A）**：微信老用户现在由
``CompanionWorldService.bootstrap_home`` 在运行时幂等带入既有角色，并照常进入选择角色页。
本脚本会把世界直接标成 ``confirmed``，等于替用户跳过选择页 —— 与 D-A 冲突。
保留它只为批量盘点与 ``--dry-run`` 对账；对未初始化的用户执行写入前必须先确认产品口径。
"""
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class BackfillReport:
    """单批 backfill 结果；只含内部 ID/计数，不含手机号或聊天正文。"""

    dry_run: bool
    scanned_users: int = 0
    zero_binding_users: int = 0
    mapped_users: int = 0
    mapped_bindings: int = 0
    full_capacity_users: int = 0
    already_initialized_users: int = 0
    errors: List[dict] = field(default_factory=list)
    next_resume_after: Optional[str] = None


def _list_user_ids(
    *, batch_size: int, created_before: Optional[str], resume_after: Optional[str]
) -> List[str]:
    from app.db import connect

    clauses = []
    params = []
    if created_before:
        clauses.append("created_at <= ?")
        params.append(created_before)
    if resume_after:
        clauses.append("id > ?")
        params.append(resume_after)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(batch_size), 5000)))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM platform_users {where} ORDER BY id ASC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _inspect_user(platform_user_id: str) -> tuple:
    """统计真人在朝夕产品内的 active legacy bindings 与世界初始化状态。"""
    from app.bootstrap.product_registry import ZHAOXI_APP_ID
    from app.db import connect

    with connect() as conn:
        binding_count = int(
            conn.execute(
                """
                SELECT COUNT(*) c
                FROM account_owner_bindings b
                JOIN accounts a ON a.id=b.account_id
                WHERE b.platform_user_id=? AND b.status='active'
                  AND b.app_id=? AND a.app_id=? AND a.status='active'
                """,
                (platform_user_id, ZHAOXI_APP_ID, ZHAOXI_APP_ID),
            ).fetchone()["c"]
        )
        world = conn.execute(
            "SELECT id FROM universes WHERE owner_platform_user_id=?",
            (platform_user_id,),
        ).fetchone()
        nonlegacy = 0
        if world is not None:
            nonlegacy = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM universe_residents "
                    "WHERE universe_id=? AND origin<>'legacy'",
                    (world["id"],),
                ).fetchone()["c"]
            )
    return binding_count, nonlegacy


def run_backfill(
    *,
    dry_run: bool,
    batch_size: int = 500,
    created_before: Optional[str] = None,
    resume_after: Optional[str] = None,
) -> BackfillReport:
    """处理一个可恢复批次；每个 platform_user 独立事务，单用户失败不影响其他人。"""
    from app.products.zhaoxi.application import SqlCompanionWorldRepository

    user_ids = _list_user_ids(
        batch_size=batch_size,
        created_before=created_before,
        resume_after=resume_after,
    )
    report = BackfillReport(dry_run=dry_run, scanned_users=len(user_ids))
    repository = SqlCompanionWorldRepository()
    for user_id in user_ids:
        try:
            binding_count, nonlegacy_count = _inspect_user(user_id)
            if nonlegacy_count:
                report.already_initialized_users += 1
                report.errors.append(
                    {"platform_user_id": user_id, "code": "nonlegacy_world_already_initialized"}
                )
                continue
            if binding_count > 10:
                report.errors.append(
                    {"platform_user_id": user_id, "code": "active_bindings_over_capacity"}
                )
                continue
            if binding_count == 0:
                report.zero_binding_users += 1
            else:
                report.mapped_users += 1
                report.mapped_bindings += binding_count
                if binding_count == 10:
                    report.full_capacity_users += 1
            if dry_run:
                continue

            with repository.transaction() as tx:
                world = tx.get_or_create_home_universe(user_id)
                world = tx.lock_universe(world.id)
                account_ids = tuple(tx.list_active_legacy_account_ids(user_id))
                # 读写间 binding 变化时，以锁内重读为准并再次强制容量边界。
                if len(account_ids) > 10:
                    raise ValueError("active_bindings_over_capacity")
                if not account_ids:
                    continue
                for account_id in account_ids:
                    tx.ensure_legacy_resident(world, account_id)
                tx.mark_legacy_world(world.id, account_ids[0])
        except Exception as err:
            report.errors.append(
                {"platform_user_id": user_id, "code": str(err)[:160]}
            )
    report.next_resume_after = user_ids[-1] if user_ids else resume_after
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Companion World legacy backfill")
    parser.add_argument("--dry-run", action="store_true", help="只读盘点，不建 world/resident")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--created-before", default=None, help="包含边界的 DB 时间串")
    parser.add_argument("--resume-after", default=None, help="从该 platform_user_id 之后继续")
    parser.add_argument("--database-url", default=None, help="可选覆盖 DATABASE_URL")
    args = parser.parse_args()
    if args.database_url is not None:
        from app.config import settings

        settings.database_url = args.database_url
    report = run_backfill(
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        created_before=args.created_before,
        resume_after=args.resume_after,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
