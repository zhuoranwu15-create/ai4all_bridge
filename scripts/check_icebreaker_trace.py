"""主动破冰 trace 检查脚本。

把 outbound_messages / icebreaker_impressions / metadata_json 里分散的字段
拼成一张可读表，用于小流量试用结果分析。

用法：
    .venv/bin/python3 scripts/check_icebreaker_trace.py
    .venv/bin/python3 scripts/check_icebreaker_trace.py --account aid_491823504
    .venv/bin/python3 scripts/check_icebreaker_trace.py --days 7
    .venv/bin/python3 scripts/check_icebreaker_trace.py --limit 50 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from app.db._core import connect


def _j(meta: Optional[str], *keys: str) -> Any:
    """从 metadata_json 里按点路径取值，失败返回 None。"""
    if not meta:
        return None
    try:
        obj = json.loads(meta)
    except (ValueError, TypeError):
        return None
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def fetch_rows(
    *,
    account_id: Optional[str] = None,
    days: int = 30,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = [
        "om.product_category = 'proactive_icebreaker'",
        f"om.created_at >= datetime('now', '+8 hours', '-{days} days')",
    ]
    params: list = []
    if account_id:
        clauses.append("om.account_id = ?")
        params.append(account_id)
    params.append(limit)

    sql = f"""
        SELECT
            om.id                   AS outbound_id,
            om.account_id,
            om.created_at,
            om.status,
            om.policy_reason,
            om.metadata_json,
            ii.id                   AS impression_id,
            ii.replied,
            ii.reply_within_hours,
            ii.continued_conversation,
            ii.negative_signal
        FROM outbound_messages om
        LEFT JOIN icebreaker_impressions ii
            ON ii.outbound_message_id = om.id
        WHERE {" AND ".join(clauses)}
        ORDER BY om.created_at DESC
        LIMIT ?
    """
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def format_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        m = r.get("metadata_json")
        out.append({
            # 时间 & 账号
            "created_at":           r["created_at"],
            "account_id":           r["account_id"],
            # L1 Trigger
            "trigger_source":       _j(m, "decision_trace", "l1_trigger", "trigger_source"),
            # L0 Context
            "last_icebreaker_at":   _j(m, "decision_trace", "l0_context", "last_icebreaker_at"),
            "last_category":        _j(m, "decision_trace", "l0_context", "last_category"),
            # L3 How
            "script_id":            _j(m, "decision_trace", "l3_how", "script_id"),
            "script_type":          _j(m, "decision_trace", "l3_how", "script_type"),
            "marketing_feel":       _j(m, "decision_trace", "l3_how", "marketing_feel"),
            "reply_cost":           _j(m, "decision_trace", "l3_how", "reply_cost"),
            "freq_tier":            _j(m, "decision_trace", "l3_how", "freq_tier"),
            # L2 When（现有字段）
            "status":               r["status"],
            "policy_reason":        r["policy_reason"],
            # L4 Safety（现有字段）
            "moderation_risk":      _j(m, "moderation_risk_level"),
            "moderation_blocked":   _j(m, "moderation_sync_blocked"),
            # L5 Outcome（impression 回填）
            "replied":              r.get("replied"),
            "reply_within_hours":   r.get("reply_within_hours"),
            "continued_conv":       r.get("continued_conversation"),
            "negative_signal":      r.get("negative_signal"),
            # raw ids
            "outbound_id":          r["outbound_id"],
            "impression_id":        r.get("impression_id"),
        })
    return out


def print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("（无记录）")
        return

    # 固定列顺序
    cols = [
        "created_at", "account_id", "status", "policy_reason",
        "trigger_source", "last_icebreaker_at", "last_category",
        "script_id", "script_type", "marketing_feel", "reply_cost", "freq_tier",
        "moderation_risk", "moderation_blocked",
        "replied", "reply_within_hours", "continued_conv", "negative_signal",
    ]

    def _str(v: Any) -> str:
        if v is None:
            return "-"
        return str(v)

    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(_str(r.get(c))))

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print("  ".join(_str(r.get(c)).ljust(widths[c]) for c in cols))

    print(f"\n共 {len(rows)} 条记录")
    # 简单汇总
    sent = sum(1 for r in rows if r["status"] == "sent")
    cancelled = sum(1 for r in rows if r["status"] == "cancelled")
    replied = sum(1 for r in rows if r.get("replied") == 1)
    negative = sum(1 for r in rows if r.get("negative_signal") == 1)
    print(f"  sent={sent}  cancelled={cancelled}  replied={replied}  negative_signal={negative}")


def main() -> None:
    parser = argparse.ArgumentParser(description="主动破冰 trace 检查")
    parser.add_argument("--account", default=None, help="只看指定账号")
    parser.add_argument("--days", type=int, default=30, help="最近 N 天（默认 30）")
    parser.add_argument("--limit", type=int, default=100, help="最多返回 N 条（默认 100）")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    rows = fetch_rows(account_id=args.account, days=args.days, limit=args.limit)
    formatted = format_rows(rows)

    if args.as_json:
        json.dump(formatted, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_table(formatted)


if __name__ == "__main__":
    main()
