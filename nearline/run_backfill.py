"""入口：回溯补跑历史区间。

先重建 fct_/dim_ 基线（增量只追加，天然到最新），再逐日重算四域 agg_（INSERT OR REPLACE）。
用法：
  python nearline/run_backfill.py --from 2026-05-30 --to 2026-06-07
  python nearline/run_backfill.py --from 2026-05-30 --to 2026-06-07 --write-reports

注意（源 schema 局限）：回溯能重算 agg_，但填不回历史里程碑/未捕捉的回复归因——
fct_proactive_message 的归因与 fct_onboarding_journey 的中间里程碑是逐日捕捉的，
回溯不会凭空补出上线前已越过的状态。
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nearline.analytics import quality  # noqa: E402
from nearline.analytics.metrics import daily_users, dreaming, onboarding, proactive  # noqa: E402
from nearline.analytics.warehouse import etl  # noqa: E402
from nearline.reporting import formatter  # noqa: E402
from nearline.reporting.scope import resolve_scope  # noqa: E402

REPORTS_DIR = Path(__file__).resolve().parent / "data" / "reports"


def _daterange(start: str, end: str):
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        raise SystemExit(f"--to {end} 早于 --from {start}")
    d = d0
    while d <= d1:
        yield d.isoformat()
        d += timedelta(days=1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nearline 回溯补跑")
    parser.add_argument("--from", dest="date_from", required=True, help="起始日 YYYY-MM-DD（含）")
    parser.add_argument("--to", dest="date_to", required=True, help="结束日 YYYY-MM-DD（含）")
    parser.add_argument("--source-db", default=None, help="覆盖操作库路径")
    parser.add_argument("--facts-db", default=None,
                        help="覆盖 facts 库路径（配合 --source-db 使用，避免污染生产 facts）")
    parser.add_argument("--write-reports", action="store_true", help="逐日落 Markdown 报告文件")
    parser.add_argument("--product", default="zhaoxi", help="服务端注册的产品 app_id")
    parser.add_argument("--channel", default="openclaw-weixin", help="服务端注册的渠道")
    args = parser.parse_args(argv)
    try:
        scope = resolve_scope(args.product, args.channel)
    except ValueError as err:
        parser.error(str(err))

    # 1) 重建基线一次。
    stats = etl.refresh_all(args.source_db, facts_db_override=args.facts_db)
    print(f"[etl] {stats}", file=sys.stderr)

    # 2) 逐日重算 agg_（软质量提示只打印，不阻断回溯）。
    if args.write_reports:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for target in _daterange(args.date_from, args.date_to):
        soft_notes = []
        summary = quality.summarize(
            quality.run_checks(
                target, source_db_override=args.source_db, facts_db_override=args.facts_db
            )
        )
        if not summary["hard_ok"]:
            failed = ", ".join(r.name for r in summary["hard_failed"])
            print(f"[{target}] 硬质量失败（仍重算 agg，请排查）：{failed}", file=sys.stderr)
        soft_notes = [f"{r.name}: {r.detail}" for r in summary["soft_failed"]]

        sections = {
            "users": daily_users.compute(target, facts_db_override=args.facts_db, scope=scope),
            "proactive": proactive.compute(target, facts_db_override=args.facts_db, scope=scope),
            "dreaming": dreaming.compute(
                target,
                source_db_override=args.source_db,
                facts_db_override=args.facts_db,
                scope=scope,
            ),
            "onboarding": onboarding.compute(target, facts_db_override=args.facts_db, scope=scope),
        }
        u = sections["users"]
        print(
            f"[{target}] DAU={u['dau']} 新增={u['new_users']} 入站={u['inbound_messages']} "
            f"主动sent={sections['proactive']['total_sent']} dreaming={sections['dreaming']['runs_total']}"
        )
        if args.write_reports:
            report = formatter.render_daily(sections, quality_notes=soft_notes, scope=scope)
            (REPORTS_DIR / f"daily_{scope.key}_{target}.md").write_text(report, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
