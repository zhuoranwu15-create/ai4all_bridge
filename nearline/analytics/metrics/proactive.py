"""分析域2：主动消息。

只读 fct_proactive_message（回复已在 ETL 归因，直接读列），join dim_account 排除 debug。
写 agg_daily_proactive / agg_hourly_proactive。
口径（foundation §4.2）：分类别独立看回复率；回复率分母只用窗口已闭合（resolution='resolved'）
的 sent，避免把尚未定论的 pending 计入；延迟用 P50。
"""

import json
from datetime import datetime
from statistics import median
from typing import Dict, List, Optional

from nearline.analytics.facts_db import connect_facts
from nearline.analytics.marts_db import connect_marts, init_marts


def _p50(values: List[int]) -> Optional[int]:
    return int(median(values)) if values else None


def compute(target_date: str, facts_db_override: Optional[str] = None) -> Dict:
    """计算并落库某日主动消息指标（日 + 分时段）。"""
    init_marts()
    facts = connect_facts(facts_db_override)
    marts = connect_marts()
    try:
        d = target_date

        # 当日所有 sent 行（join 排除 debug）。
        sent_rows = facts.execute(
            "SELECT p.category, p.sent_hour, p.replied, p.reply_latency_sec, "
            " p.resolution_status, p.reply_window_hours "
            "FROM fct_proactive_message p JOIN dim_account a ON p.account_id = a.account_id "
            "WHERE p.sent_date = ? AND p.status = 'sent' AND a.is_debug = 0",
            (d,),
        ).fetchall()

        total_sent = len(sent_rows)
        covered = facts.execute(
            "SELECT COUNT(DISTINCT p.account_id) AS c "
            "FROM fct_proactive_message p JOIN dim_account a ON p.account_id = a.account_id "
            "WHERE p.sent_date = ? AND p.status = 'sent' AND a.is_debug = 0",
            (d,),
        ).fetchone()["c"]

        # blocked：有 policy_reason 且非 sent，按 created_date 归属。
        blocked = facts.execute(
            "SELECT COUNT(*) AS c "
            "FROM fct_proactive_message p JOIN dim_account a ON p.account_id = a.account_id "
            "WHERE p.created_date = ? AND p.status != 'sent' AND p.policy_reason IS NOT NULL "
            "AND a.is_debug = 0",
            (d,),
        ).fetchone()["c"]

        # 回复率分母：窗口已闭合的 sent。
        resolved = [r for r in sent_rows if r["resolution_status"] == "resolved"]
        resolved_sent = len(resolved)
        replied_total = sum(1 for r in resolved if r["replied"])
        reply_rate = round(replied_total / resolved_sent, 4) if resolved_sent else None
        latencies = [r["reply_latency_sec"] for r in resolved if r["replied"] and r["reply_latency_sec"] is not None]
        latency_p50 = _p50(latencies)
        window_hours = next((r["reply_window_hours"] for r in sent_rows if r["reply_window_hours"]), None)

        # 分类别：sent / resolved / replied / reply_rate。
        by_cat: Dict[str, Dict] = {}
        for r in sent_rows:
            cat = r["category"] or "unknown"
            slot = by_cat.setdefault(cat, {"sent": 0, "resolved": 0, "replied": 0})
            slot["sent"] += 1
            if r["resolution_status"] == "resolved":
                slot["resolved"] += 1
                if r["replied"]:
                    slot["replied"] += 1
        for slot in by_cat.values():
            slot["reply_rate"] = (
                round(slot["replied"] / slot["resolved"], 4) if slot["resolved"] else None
            )

        marts.execute(
            "INSERT OR REPLACE INTO agg_daily_proactive"
            "(date, total_sent, blocked_count, covered_accounts, replied_total, resolved_sent, "
            " reply_rate_overall, reply_latency_p50_sec, reply_window_hours, by_category_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d, total_sent, blocked, covered, replied_total, resolved_sent, reply_rate,
                latency_p50, window_hours, json.dumps(by_cat, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

        # 分时段（按 sent_hour）。
        hourly: Dict[int, Dict] = {}
        for r in sent_rows:
            h = r["sent_hour"]
            if h is None:
                continue
            slot = hourly.setdefault(h, {"sent": 0, "resolved": 0, "replied": 0})
            slot["sent"] += 1
            if r["resolution_status"] == "resolved":
                slot["resolved"] += 1
                if r["replied"]:
                    slot["replied"] += 1
        for h, slot in hourly.items():
            rate = round(slot["replied"] / slot["resolved"], 4) if slot["resolved"] else None
            marts.execute(
                "INSERT OR REPLACE INTO agg_hourly_proactive"
                "(date, hour, sent, resolved, replied, reply_rate) VALUES (?, ?, ?, ?, ?, ?)",
                (d, h, slot["sent"], slot["resolved"], slot["replied"], rate),
            )
        marts.commit()

        return {
            "date": d,
            "total_sent": total_sent,
            "blocked_count": blocked,
            "covered_accounts": covered,
            "resolved_sent": resolved_sent,
            "replied_total": replied_total,
            "reply_rate_overall": reply_rate,
            "reply_latency_p50_sec": latency_p50,
            "reply_window_hours": window_hours,
            "by_category": by_cat,
        }
    finally:
        facts.close()
        marts.close()
