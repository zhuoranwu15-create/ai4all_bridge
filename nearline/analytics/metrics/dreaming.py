"""分析域3：Dreaming 运行情况。

读 facts 的 fct_dreaming_run / fct_dreaming_memory_item（join dim_account 排除 debug），
外加操作库 scheduler_heartbeats 的当前快照（调度健康，唯一的操作库读，非历史指标）。
写 agg_daily_dreaming。口径：partial 独立计、token NULL 安全（foundation §4.3）。
"""

import json
from datetime import datetime
from typing import Dict, Optional

from nearline.analytics.facts_db import connect_facts
from nearline.analytics.marts_db import connect_marts, init_marts
from nearline.analytics.source_db import connect_source


def _scheduler_health(source_db_override: Optional[str]) -> Dict[str, Optional[str]]:
    """读取 dreaming_scheduler 当前 heartbeat（健康快照）。"""
    try:
        src = connect_source(source_db_override)
    except FileNotFoundError:
        return {"status": None, "last_success_at": None}
    try:
        row = src.execute(
            "SELECT status, last_success_at FROM scheduler_heartbeats WHERE service = ?",
            ("dreaming_scheduler",),
        ).fetchone()
        if not row:
            return {"status": None, "last_success_at": None}
        return {"status": row["status"], "last_success_at": row["last_success_at"]}
    finally:
        src.close()


def compute(
    target_date: str,
    source_db_override: Optional[str] = None,
    facts_db_override: Optional[str] = None,
) -> Dict:
    """计算并落库某日 Dreaming 指标。"""
    init_marts()
    facts = connect_facts(facts_db_override)
    marts = connect_marts()
    try:
        d = target_date
        runs = facts.execute(
            "SELECT "
            " COUNT(*) AS total, "
            " SUM(CASE WHEN r.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded, "
            " SUM(CASE WHEN r.status = 'partial' THEN 1 ELSE 0 END) AS partial, "
            " SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            " COUNT(DISTINCT r.account_id) AS accounts, "
            " AVG(r.duration_sec) AS avg_dur, "
            " SUM(r.token_input) AS tok_in, "
            " SUM(r.token_output) AS tok_out "
            "FROM fct_dreaming_run r JOIN dim_account a ON r.account_id = a.account_id "
            "WHERE r.run_date = ? AND a.is_debug = 0",
            (d,),
        ).fetchone()

        items = facts.execute(
            "SELECT "
            " COUNT(*) AS generated, "
            " SUM(CASE WHEN i.apply_status = 'applied' THEN 1 ELSE 0 END) AS applied, "
            " SUM(CASE WHEN i.apply_status = 'skipped' THEN 1 ELSE 0 END) AS skipped "
            "FROM fct_dreaming_memory_item i JOIN dim_account a ON i.account_id = a.account_id "
            "WHERE i.event_date = ? AND a.is_debug = 0",
            (d,),
        ).fetchone()

        skip_rows = facts.execute(
            "SELECT COALESCE(i.skip_reason, 'unknown') AS reason, COUNT(*) AS c "
            "FROM fct_dreaming_memory_item i JOIN dim_account a ON i.account_id = a.account_id "
            "WHERE i.event_date = ? AND a.is_debug = 0 AND i.apply_status = 'skipped' "
            "GROUP BY 1",
            (d,),
        ).fetchall()
        by_skip = {r["reason"]: r["c"] for r in skip_rows}

        generated = items["generated"] or 0
        applied = items["applied"] or 0
        skipped = items["skipped"] or 0
        applied_rate = round(applied / generated, 4) if generated else None
        avg_dur = round(runs["avg_dur"], 2) if runs["avg_dur"] is not None else None

        health = _scheduler_health(source_db_override)

        result = {
            "date": d,
            "runs_total": runs["total"] or 0,
            "runs_succeeded": runs["succeeded"] or 0,
            "runs_partial": runs["partial"] or 0,
            "runs_failed": runs["failed"] or 0,
            "accounts_covered": runs["accounts"] or 0,
            "avg_duration_sec": avg_dur,
            "tokens_input": runs["tok_in"],
            "tokens_output": runs["tok_out"],
            "items_generated": generated,
            "items_applied": applied,
            "items_skipped": skipped,
            "items_applied_rate": applied_rate,
            "items_by_skip_reason": by_skip,
            "scheduler_status": health["status"],
            "scheduler_last_success_at": health["last_success_at"],
        }

        marts.execute(
            "INSERT OR REPLACE INTO agg_daily_dreaming"
            "(date, runs_total, runs_succeeded, runs_partial, runs_failed, accounts_covered, "
            " avg_duration_sec, tokens_input, tokens_output, items_generated, items_applied, "
            " items_skipped, items_applied_rate, items_by_skip_reason_json, scheduler_status, "
            " scheduler_last_success_at, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d, result["runs_total"], result["runs_succeeded"], result["runs_partial"],
                result["runs_failed"], result["accounts_covered"], avg_dur,
                result["tokens_input"], result["tokens_output"], generated, applied, skipped,
                applied_rate, json.dumps(by_skip, ensure_ascii=False),
                health["status"], health["last_success_at"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        marts.commit()
        return result
    finally:
        facts.close()
        marts.close()
