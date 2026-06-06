#!/usr/bin/env python
"""回填历史 Dreaming 长期记忆。

背景：早期 prompt/schema 未告知模型 sensitivity 合法枚举，模型返回的非法值被
归一化兜底成 "sensitive"，导致蒸馏出的长期记忆条目在自动应用阶段被全部以
skip_reason='sensitive_item' 丢弃，MEMORY.md / USER.md 一直为空。

本脚本把这些被误判的条目（apply_status='skipped' 且 skip_reason='sensitive_item'）
的 sensitivity 重置为 normal 后，按现行规则重新评估并应用。PII 正则与置信度/重要性
门槛仍然生效，真正敏感的条目不会被写入。

默认 dry-run，只打印将要发生什么；确认无误后加 --apply 真正写入。

用法：
    # 预演（不写任何东西），先看决策
    .venv/bin/python scripts/backfill_dreaming_memory.py

    # 只看某个账号
    .venv/bin/python scripts/backfill_dreaming_memory.py --account aid_530877813

    # 确认后真正回填
    .venv/bin/python scripts/backfill_dreaming_memory.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.dreaming import reapply_sensitivity_misskips


def _preview(text: str, width: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description="回填被误判为敏感而丢弃的长期记忆")
    parser.add_argument("--account", default=None, help="只处理该 account_id（默认全部）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则仅预演")
    parser.add_argument("--limit", type=int, default=500, help="最多处理的条目数")
    args = parser.parse_args()

    mode = "APPLY（将写入文件与 DB）" if args.apply else "DRY-RUN（仅预演）"
    print(f"模式: {mode}")
    print(f"数据库: {settings.database_path}")
    print(f"记忆目录: {settings.user_profiles_dir}")
    print(f"账号过滤: {args.account or '全部'}\n")

    result = reapply_sensitivity_misskips(
        account_id=args.account,
        apply=args.apply,
        limit=args.limit,
    )

    for d in result["decisions"]:
        flag = {
            "applied": "✓ 已写入",
            "would_apply": "→ 将写入",
        }.get(d["status"], f"· {d['status']}")
        reason = f" [{d['skip_reason']}]" if d["skip_reason"] else ""
        print(
            f"{flag}{reason}  {d['account_id']} {d['target_file']} "
            f"imp={d['importance']} conf={d['confidence']}  {_preview(d['memory_text'])}"
        )

    print("\n===== 汇总 =====")
    print(f"候选(误判 sensitive_item): {result['candidates']}")
    if args.apply:
        print(f"已应用: {result['applied']}")
        print(f"仍跳过: {result['skipped']}（PII/低置信/低重要性等，符合预期）")
    else:
        print(f"将应用: {result['would_apply']}")
        print(f"仍跳过: {result['skipped']}（PII/低置信/低重要性等，符合预期）")
        print("\n如确认无误，重新运行并加 --apply 以真正回填。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
