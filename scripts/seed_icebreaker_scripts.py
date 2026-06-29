"""把 data/seeds/icebreaker_scripts.csv 导入 icebreaker_scripts 表。

用法：
  python scripts/seed_icebreaker_scripts.py \\
      --db data/ai4all.sqlite3 \\
      --csv data/seeds/icebreaker_scripts.csv

  --dry-run  只打印统计，不写数据库
  --update   改用 UPSERT（INSERT OR REPLACE），允许更新已有行

设计原则：
- 使用 stdlib csv，零新依赖
- 导入前校验，失败即中断
- 默认 INSERT OR IGNORE（幂等）
- freq_tier 未知值报错，不静默导入
"""

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VALID_FREQ_TIERS = {"common", "mid_low", "low_freq"}
VALID_SCRIPT_TYPES = {"小测试", "安全吐槽", "生活观察", "假设题", "轻八卦", "关系提问"}
SCORE_FIELDS = ("fun_score", "reply_ease_score", "offense_risk", "marketing_feel")


# ---------------------------------------------------------------------------
# 核心函数（可被测试直接调用）
# ---------------------------------------------------------------------------

def load_seed_rows(csv_path: str) -> List[Dict[str, str]]:
    """读取 CSV，返回行列表（均为字符串，未做类型转换）。"""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {csv_path}")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows


def validate_seed_rows(rows: List[Dict[str, str]]) -> None:
    """校验行数据，发现问题即 raise ValueError。

    检查项：
    - 正好 100 行
    - id 不重复
    - text / script_type 非空
    - freq_tier 必须是 common / mid_low / low_freq
    - enabled 必须是 0 或 1
    - 四项评分字段必须是 1-5 整数
    """
    if len(rows) != 100:
        raise ValueError(f"CSV 应有 100 行数据，实际 {len(rows)} 行")

    seen_ids: set = set()
    errors: List[str] = []

    for i, row in enumerate(rows, start=2):  # CSV 行号从 2（跳过 header）
        row_id = row.get("id", "").strip()

        # id 唯一性
        if not row_id:
            errors.append(f"行 {i}: id 为空")
        elif row_id in seen_ids:
            errors.append(f"行 {i}: id 重复 {row_id!r}")
        else:
            seen_ids.add(row_id)

        # text 非空
        if not row.get("text", "").strip():
            errors.append(f"行 {i} ({row_id}): text 为空")

        # script_type 非空
        if not row.get("script_type", "").strip():
            errors.append(f"行 {i} ({row_id}): script_type 为空")

        # freq_tier
        freq = row.get("freq_tier", "").strip()
        if freq not in VALID_FREQ_TIERS:
            errors.append(
                f"行 {i} ({row_id}): freq_tier={freq!r} 非法，"
                f"合法值: {sorted(VALID_FREQ_TIERS)}"
            )

        # enabled
        enabled = row.get("enabled", "").strip()
        if enabled not in ("0", "1"):
            errors.append(f"行 {i} ({row_id}): enabled={enabled!r} 非法，必须为 0 或 1")

        # 评分字段
        for field in SCORE_FIELDS:
            val = row.get(field, "").strip()
            if val == "":
                # 允许空（NULL）
                continue
            try:
                iv = int(val)
            except ValueError:
                errors.append(f"行 {i} ({row_id}): {field}={val!r} 不是整数")
                continue
            if not (1 <= iv <= 5):
                errors.append(f"行 {i} ({row_id}): {field}={iv} 不在 1-5 范围内")

    if errors:
        msg = f"CSV 校验失败，共 {len(errors)} 处错误：\n" + "\n".join(errors)
        raise ValueError(msg)


def _to_int_or_none(val: str) -> Optional[int]:
    v = val.strip()
    if not v:
        return None
    return int(v)


def seed_icebreaker_scripts(
    db_path: str,
    csv_path: str,
    update: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """导入 CSV 到 icebreaker_scripts 表，返回统计字典。

    Args:
        db_path: SQLite 数据库路径
        csv_path: CSV 文件路径
        update: True 时改用 INSERT OR REPLACE（upsert），允许覆盖已有行
        dry_run: True 时只校验+统计，不写数据库

    Returns:
        包含 total/enabled/disabled/inserted/skipped/updated/
        type_dist/freq_dist/marketing_dist 的统计字典
    """
    rows = load_seed_rows(csv_path)
    validate_seed_rows(rows)

    total = len(rows)
    enabled_count = sum(1 for r in rows if r["enabled"] == "1")
    disabled_count = total - enabled_count
    type_dist = dict(Counter(r["script_type"] for r in rows))
    freq_dist = dict(Counter(r["freq_tier"] for r in rows))
    marketing_dist = dict(Counter(r["marketing_feel"] for r in rows))

    stats: Dict[str, Any] = {
        "total": total,
        "enabled": enabled_count,
        "disabled": disabled_count,
        "inserted": 0,
        "skipped": 0,
        "updated": 0,
        "type_dist": type_dist,
        "freq_dist": freq_dist,
        "marketing_dist": marketing_dist,
    }

    if dry_run:
        return stats

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        if update:
            sql = """
                INSERT OR REPLACE INTO icebreaker_scripts (
                    id, script_type, text, reply_cost, tone,
                    suitable_for, avoid_when, follow_goal, signal_extract,
                    fun_score, reply_ease_score, offense_risk, marketing_feel,
                    freq_tier, enabled, notes
                ) VALUES (
                    :id, :script_type, :text, :reply_cost, :tone,
                    :suitable_for, :avoid_when, :follow_goal, :signal_extract,
                    :fun_score, :reply_ease_score, :offense_risk, :marketing_feel,
                    :freq_tier, :enabled, :notes
                )
            """
        else:
            sql = """
                INSERT OR IGNORE INTO icebreaker_scripts (
                    id, script_type, text, reply_cost, tone,
                    suitable_for, avoid_when, follow_goal, signal_extract,
                    fun_score, reply_ease_score, offense_risk, marketing_feel,
                    freq_tier, enabled, notes
                ) VALUES (
                    :id, :script_type, :text, :reply_cost, :tone,
                    :suitable_for, :avoid_when, :follow_goal, :signal_extract,
                    :fun_score, :reply_ease_score, :offense_risk, :marketing_feel,
                    :freq_tier, :enabled, :notes
                )
            """

        for row in rows:
            params = {
                "id": row["id"],
                "script_type": row["script_type"],
                "text": row["text"],
                "reply_cost": row["reply_cost"] or None,
                "tone": row["tone"] or None,
                "suitable_for": row["suitable_for"] or None,
                "avoid_when": row["avoid_when"] or None,
                "follow_goal": row["follow_goal"] or None,
                "signal_extract": row["signal_extract"] or None,
                "fun_score": _to_int_or_none(row.get("fun_score", "")),
                "reply_ease_score": _to_int_or_none(row.get("reply_ease_score", "")),
                "offense_risk": _to_int_or_none(row.get("offense_risk", "")),
                "marketing_feel": _to_int_or_none(row.get("marketing_feel", "")),
                "freq_tier": row["freq_tier"],
                "enabled": int(row["enabled"]),
                "notes": row["notes"] or None,
            }
            cur = conn.execute(sql, params)
            if update:
                stats["updated"] += 1
            elif cur.rowcount > 0:
                stats["inserted"] += 1
            else:
                stats["skipped"] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return stats


# ---------------------------------------------------------------------------
# 统计输出
# ---------------------------------------------------------------------------

def print_stats(stats: Dict[str, Any], dry_run: bool, update: bool) -> None:
    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{mode}导入统计")
    print(f"  total rows:    {stats['total']}")
    print(f"  enabled:       {stats['enabled']}")
    print(f"  disabled:      {stats['disabled']}")
    if not dry_run:
        if update:
            print(f"  updated:       {stats['updated']}")
        else:
            print(f"  inserted:      {stats['inserted']}")
            print(f"  skipped:       {stats['skipped']}")
    print(f"\n  script_type 分布:")
    for k, v in sorted(stats["type_dist"].items()):
        print(f"    {k}: {v}")
    print(f"\n  freq_tier 分布:")
    for k, v in sorted(stats["freq_dist"].items()):
        print(f"    {k}: {v}")
    print(f"\n  marketing_feel 分布:")
    for k, v in sorted(stats["marketing_dist"].items()):
        print(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="将 icebreaker_scripts.csv 导入 SQLite icebreaker_scripts 表"
    )
    parser.add_argument(
        "--db",
        default="data/ai4all.sqlite3",
        help="SQLite 数据库路径（默认: data/ai4all.sqlite3）",
    )
    parser.add_argument(
        "--csv",
        default="data/seeds/icebreaker_scripts.csv",
        help="CSV 文件路径（默认: data/seeds/icebreaker_scripts.csv）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验和统计，不写数据库",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="改用 INSERT OR REPLACE，允许更新已有行（默认 INSERT OR IGNORE）",
    )
    args = parser.parse_args()

    try:
        stats = seed_icebreaker_scripts(
            db_path=args.db,
            csv_path=args.csv,
            update=args.update,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    print_stats(stats, dry_run=args.dry_run, update=args.update)

    if args.dry_run:
        print("\n[DRY-RUN] 未写入数据库。")
    else:
        print(f"\n完成。数据库: {args.db}")


if __name__ == "__main__":
    main()
