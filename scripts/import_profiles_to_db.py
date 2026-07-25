#!/usr/bin/env python
"""One-time import: existing on-disk profile files -> account_profile_files (P2).

厚节点改造 P2 把账号级 profile 文件（SOUL/IDENTITY/USER/MEMORY/user_profile.md 及
memory/YYYY-MM-DD.md daily notes）的真相从磁盘搬进 account_profile_files 表（profile_storage）。
切换后 storage 起始为空——本脚本把存量磁盘文件一次性导入，否则 app 会按默认模板重建、丢失历史。

storage 的 key 是**原始 account_id**，而磁盘目录名是 _safe_account_dir_name() 净化后的结果
（对真实 account_id 是恒等，但理论有损）。因此优先用 DB 里的原始 account_id 反推磁盘目录；
对 DB 中不存在的孤儿目录，以目录名兜底为 account_id 并标注 unknown，由运维确认。

默认 dry-run：打印每个账号将导入/跳过的文件清单；确认后加 --apply 才真正写库。
默认只导入 storage 中尚不存在的文件（幂等、不覆盖切换后产生的新写入）；--overwrite 才整文件覆盖。

Usage:
    .venv/bin/python scripts/import_profiles_to_db.py
    .venv/bin/python scripts/import_profiles_to_db.py --account aid_806382741
    .venv/bin/python scripts/import_profiles_to_db.py --apply
    .venv/bin/python scripts/import_profiles_to_db.py --apply --overwrite
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_runtime.persistence import profile_storage
from app.config import settings
from app.db import init_db, list_accounts
from app.user_profiles import _safe_account_dir_name

# 状态枚举：新增 / 已存在跳过 / 已存在覆盖。
STATUS_NEW = "new"
STATUS_SKIP_EXISTS = "exists-skip"
STATUS_OVERWRITE = "exists-overwrite"


@dataclass(frozen=True)
class ImportItem:
    """单个待导入文件：filename 为账号目录内相对 posix 路径，直接作为 storage key。"""

    filename: str
    content: str
    status: str


@dataclass
class AccountImportPlan:
    """单个账号的导入计划。known_account 标识该账号是否能在 DB 中找到原始 id。"""

    account_id: str
    directory: Path
    known_account: bool
    items: List[ImportItem] = field(default_factory=list)

    @property
    def to_write(self) -> List[ImportItem]:
        return [it for it in self.items if it.status != STATUS_SKIP_EXISTS]


def _iter_profile_files(account_dir: Path) -> Iterable[Path]:
    """遍历账号目录下所有常规文件（含 memory/*.md），跳过 __pycache__/隐藏文件。"""
    for path in sorted(account_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(account_dir).parts
        if any(part == "__pycache__" or part.startswith(".") for part in rel_parts):
            continue
        yield path


def _db_reverse_map(db_account_ids: Iterable[str]) -> Dict[str, str]:
    """{净化后的目录名: 原始 account_id}，用于从磁盘目录反推权威原始 id。"""
    return {_safe_account_dir_name(aid): aid for aid in db_account_ids}


def plan_account_import(
    account_id: str,
    account_dir: Path,
    *,
    known_account: bool,
    overwrite: bool = False,
) -> AccountImportPlan:
    """为单个账号目录构建导入计划，不写库；读 storage 判定每个文件的导入/跳过状态。"""
    plan = AccountImportPlan(account_id=account_id, directory=account_dir, known_account=known_account)
    for path in _iter_profile_files(account_dir):
        filename = path.relative_to(account_dir).as_posix()  # storage key：账号目录内相对路径
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            print(f"  ! 跳过无法读取的文件 {path}: {err}", file=sys.stderr)
            continue
        if profile_storage.exists(account_id, filename):
            status = STATUS_OVERWRITE if overwrite else STATUS_SKIP_EXISTS
        else:
            status = STATUS_NEW
        plan.items.append(ImportItem(filename=filename, content=content, status=status))
    return plan


def collect_import_plans(
    profiles_dir: Path,
    *,
    account_filter: Optional[str] = None,
    overwrite: bool = False,
    db_account_ids: Optional[Iterable[str]] = None,
) -> List[AccountImportPlan]:
    """扫描 profiles_dir 下所有账号目录，构建导入计划列表（不写库）。

    account_filter 指定时只处理该原始 account_id（按净化规则定位其磁盘目录）。
    db_account_ids 提供 DB 中的原始 id 全集，用于把净化目录名还原为权威原始 id。
    """
    profiles_dir = Path(profiles_dir)
    reverse_map = _db_reverse_map(db_account_ids or [])

    if account_filter is not None:
        account_dir = profiles_dir / _safe_account_dir_name(account_filter)
        if not account_dir.is_dir():
            return []
        known = account_filter in set(db_account_ids or [])
        return [plan_account_import(account_filter, account_dir, known_account=known, overwrite=overwrite)]

    if not profiles_dir.is_dir():
        return []

    plans: List[AccountImportPlan] = []
    for child in sorted(profiles_dir.iterdir()):
        if not child.is_dir():
            continue
        # 优先用 DB 原始 id（权威，避免净化有损）；目录在 DB 中无对应时以目录名兜底。
        account_id = reverse_map.get(child.name, child.name)
        known = child.name in reverse_map
        plans.append(plan_account_import(account_id, child, known_account=known, overwrite=overwrite))
    return plans


def apply_plan(plan: AccountImportPlan) -> int:
    """执行单个账号计划：写入所有非跳过项，返回实际写入文件数。"""
    written = 0
    for item in plan.to_write:
        profile_storage.write_file(plan.account_id, item.filename, item.content)
        written += 1
    return written


def _account_ids_from_db() -> List[str]:
    return [str(account["id"]) for account in list_accounts()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Import on-disk profile files into account_profile_files.")
    parser.add_argument("--account", default=None, help="只导入该原始 account_id（默认扫描全部目录）")
    parser.add_argument("--apply", action="store_true", help="真正写库；不加则仅 dry-run 预览")
    parser.add_argument("--overwrite", action="store_true", help="storage 已存在的文件也整文件覆盖（默认跳过）")
    args = parser.parse_args()

    init_db()  # 幂等：确保 account_profile_files 表已就绪
    db_account_ids = _account_ids_from_db()
    plans = collect_import_plans(
        Path(settings.user_profiles_dir),
        account_filter=args.account,
        overwrite=args.overwrite,
        db_account_ids=db_account_ids,
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"mode={mode}")
    print(f"database={settings.database_path}")
    print(f"user_profiles_dir={settings.user_profiles_dir}")
    print(f"account_filter={args.account or 'all-dirs'}")
    print(f"overwrite={args.overwrite}")
    print(f"db_accounts={len(db_account_ids)}")
    print(f"scanned_account_dirs={len(plans)}")

    total_new = total_overwrite = total_skip = total_written = 0
    unknown_dirs = 0
    for index, plan in enumerate(plans, start=1):
        new = [it for it in plan.items if it.status == STATUS_NEW]
        ow = [it for it in plan.items if it.status == STATUS_OVERWRITE]
        sk = [it for it in plan.items if it.status == STATUS_SKIP_EXISTS]
        total_new += len(new)
        total_overwrite += len(ow)
        total_skip += len(sk)
        if not plan.known_account:
            unknown_dirs += 1

        tag = "" if plan.known_account else " [unknown-account]"
        print("\n" + "=" * 80)
        print(
            f"[{index}/{len(plans)}] account={plan.account_id}{tag} dir={plan.directory} "
            f"files={len(plan.items)} new={len(new)} overwrite={len(ow)} skip={len(sk)}"
        )
        for item in plan.items:
            print(f"  - [{item.status}] {item.filename} ({len(item.content)} chars)")
        if args.apply:
            total_written += apply_plan(plan)

    print("\n" + "-" * 80)
    print(
        f"summary: accounts={len(plans)} unknown_account_dirs={unknown_dirs} "
        f"new={total_new} overwrite={total_overwrite} skip_existing={total_skip}"
    )
    if args.apply:
        print(f"written={total_written}")
    else:
        print("\nDry-run only. Re-run with --apply after reviewing the plan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
