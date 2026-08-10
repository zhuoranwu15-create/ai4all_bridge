#!/usr/bin/env python
"""为存量账号回填使命分配。

背景：使命子系统上线后，只有 onboarding 完成时才会自动分配使命
（app/turn_service.py 的 ONBOARDING_COMPLETE 转移点）。已绑定的存量账号
不会自动补上，需要本脚本一次性回填——主要为方便内部测试（见
docs/architecture/products/zhaoxi/agent_mission_and_orchestration_design.md §5.1/§11 的存量账号回填决定）。

幂等：复用 app.products.zhaoxi.application.missions.assignment.assign_mission_if_absent，已分配使命的
账号会被直接跳过，可安全重复运行。

默认 dry-run，只打印将要发生什么；确认无误后加 --apply 真正写入。

用法：
    # 预演（不写任何东西），先看会给哪些账号分配什么使命
    .venv/bin/python scripts/backfill_account_missions.py

    # 只看某个账号
    .venv/bin/python scripts/backfill_account_missions.py --account aid_806382741

    # 确认后真正回填
    .venv/bin/python scripts/backfill_account_missions.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.db import get_account_mission, list_accounts
from app.products.zhaoxi.application.missions.assignment import (
    assign_mission_if_absent,
    resolve_mission_assignment_candidate,
)
from app.products.zhaoxi.domain.missions.registry import get_mission_template


def main() -> int:
    parser = argparse.ArgumentParser(description="为存量账号回填使命分配")
    parser.add_argument("--account", default=None, help="只处理该 account_id（默认全部）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则仅预演")
    args = parser.parse_args()

    mode = "APPLY（将写入 MISSION.md 与 account_mission）" if args.apply else "DRY-RUN（仅预演）"
    print(f"模式: {mode}")
    print("数据库: PostgreSQL")
    print(f"账号过滤: {args.account or '全部'}\n")

    accounts = list_accounts()
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]

    assigned = 0
    already = 0
    skipped_freeform = 0
    for account in accounts:
        account_id = account["id"]
        existing = get_account_mission(account_id=account_id)
        if existing is not None:
            already += 1
            print(f"· 已分配  {account_id}  mission_id={existing['mission_id']}")
            continue

        would_be = resolve_mission_assignment_candidate(account_id=account_id)
        if would_be is None:
            skipped_freeform += 1
            print(f"· 自由使命  {account_id}  跳过量化使命分配")
            continue
        template = get_mission_template(would_be)
        if args.apply:
            mission_id = assign_mission_if_absent(account_id=account_id)
            print(f"✓ 已分配  {account_id}  mission_id={mission_id}（{template.display_name}）")
        else:
            print(f"→ 将分配  {account_id}  mission_id={would_be}（{template.display_name}）")
        assigned += 1

    print("\n===== 汇总 =====")
    print(f"账号总数: {len(accounts)}")
    print(f"已分配（跳过）: {already}")
    print(f"自由使命（跳过）: {skipped_freeform}")
    print(f"{'新分配' if args.apply else '将分配'}: {assigned}")
    if not args.apply and assigned:
        print("\n未加 --apply，未写入任何内容。确认无误后加 --apply 重新运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
