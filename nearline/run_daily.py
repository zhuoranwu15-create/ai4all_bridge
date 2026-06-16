"""入口：刷新基础表 → 数据质量门禁 → 计算四域 agg_ → 渲染 Markdown 日报。

用法：
  python -m nearline.run_daily --date 2026-06-07
  python nearline/run_daily.py --yesterday
  python nearline/run_daily.py --date 2026-06-07 --no-write     # 只打印不落文件
  python nearline/run_daily.py --date 2026-06-07 --skip-quality # 跳过质量门禁（应急）

硬质量检查失败：飞书告警 + 非零退出 + 不生成报告。
软质量检查失败：写入报告"数据质量提示"段，不阻断。
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nearline.alerting import send_alert, send_report  # noqa: E402
from nearline.analytics import quality  # noqa: E402
from nearline.analytics.metrics import daily_users, dreaming, onboarding, proactive  # noqa: E402
from nearline.analytics.warehouse import etl  # noqa: E402
from nearline.reporting import formatter  # noqa: E402

NEARLINE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = NEARLINE_DIR / "data" / "reports"
STATE_FILE = NEARLINE_DIR / "data" / "run_state.json"


def _save_state(state: dict) -> None:
    """原子写运行状态文件，供监控检测报告是否陈旧。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nearline 每日报告")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--date", help="目标日 YYYY-MM-DD（北京自然日）")
    group.add_argument("--yesterday", action="store_true", help="目标日取昨天")
    parser.add_argument("--source-db", default=None, help="覆盖操作库路径")
    parser.add_argument("--facts-db", default=None,
                        help="覆盖 facts 库路径（配合 --source-db 使用，避免污染生产 facts）")
    parser.add_argument("--no-write", action="store_true", help="只打印，不落报告文件")
    parser.add_argument("--skip-quality", action="store_true", help="跳过质量门禁（应急）")
    parser.add_argument("--no-feishu", action="store_true",
                        help="不推送飞书（手动/测试时使用；默认成功后推送运营群）")
    args = parser.parse_args(argv)

    target = (
        (date.today() - timedelta(days=1)).isoformat() if args.yesterday else args.date
    )

    stats = etl.refresh_all(args.source_db, facts_db_override=args.facts_db)
    print(f"[etl] {stats}", file=sys.stderr)

    # 数据质量门禁：硬失败阻断并告警。
    soft_notes = []
    if not args.skip_quality:
        results = quality.run_checks(
            target, source_db_override=args.source_db, facts_db_override=args.facts_db
        )
        summary = quality.summarize(results)
        for r in results:
            mark = "ok" if r.passed else "FAIL"
            print(f"[quality:{r.severity}] {r.name}: {mark} — {r.detail}", file=sys.stderr)
        if not summary["hard_ok"]:
            detail = "; ".join(f"{r.name}: {r.detail}" for r in summary["hard_failed"])
            msg = f"{target} 数据质量硬检查失败，已阻断报告：{detail}"
            send_alert(msg)
            _save_state({
                "last_run_at": datetime.now().isoformat(timespec="seconds"),
                "target_date": target,
                "status": "blocked_hard_quality",
                "etl": stats,
                "hard_failed": [r.name for r in summary["hard_failed"]],
            })
            print(f"[blocked] {msg}", file=sys.stderr)
            return 1
        soft_notes = [f"{r.name}: {r.detail}" for r in summary["soft_failed"]]

    sections = {
        "users": daily_users.compute(target, facts_db_override=args.facts_db),
        "proactive": proactive.compute(target, facts_db_override=args.facts_db),
        "dreaming": dreaming.compute(
            target,
            source_db_override=args.source_db,
            facts_db_override=args.facts_db,
        ),
        "onboarding": onboarding.compute(target, facts_db_override=args.facts_db),
    }
    report = formatter.render_daily(sections, quality_notes=soft_notes,
                                    quality_skipped=args.skip_quality)
    print(report)

    written = None
    if not args.no_write:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / f"daily_{target}.md"
        out.write_text(report, encoding="utf-8")
        written = str(out)
        print(f"[report] written {written}", file=sys.stderr)

    # 推送精简摘要到运营飞书群（best-effort，失败不影响日报产出）。
    pushed = False
    if not args.no_feishu:
        summary = formatter.render_feishu_summary(sections, quality_notes=soft_notes)
        pushed = send_report(summary)
        print(f"[feishu] pushed={pushed}", file=sys.stderr)

    _save_state({
        "last_success_at": datetime.now().isoformat(timespec="seconds"),
        "target_date": target,
        "status": "ok",
        "etl": stats,
        "soft_warnings": soft_notes,
        "report": written,
        "feishu_pushed": pushed,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
