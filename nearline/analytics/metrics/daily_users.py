"""分析域1：用户增长与留存。

只读 facts.sqlite3 的 fct_message / dim_account，写 marts.sqlite3 的 agg_daily_users。
纯聚合、幂等（INSERT OR REPLACE）。默认排除 is_debug=1。
口径事实源：analytics_foundation_design.md §4.1；留存样本 < 10 标"仅观察"（§8）。
"""

from datetime import date, datetime
from typing import Dict, Optional

from nearline.analytics.facts_db import connect_facts
from nearline.analytics.marts_db import connect_marts, init_marts

MIN_COHORT = 10  # foundation §8：cohort 样本不足则不展示比率


def compute(
    target_date: str,
    today: Optional[str] = None,
    facts_db_override: Optional[str] = None,
) -> Dict:
    """计算并落库某日的增长指标，返回指标 dict。

    target_date / today 均为 YYYY-MM-DD（北京自然日）。
    d1_status: ok（样本足且窗口闭合）| observe_only（样本<10）| window_open（次日未到）。
    """
    init_marts()
    facts = connect_facts(facts_db_override)
    marts = connect_marts()
    try:
        d = target_date
        today = today or date.today().isoformat()
        next_day = (date.fromisoformat(d).toordinal() + 1)
        next_day = date.fromordinal(next_day).isoformat()

        new_users = facts.execute(
            "SELECT COUNT(*) AS c FROM dim_account "
            "WHERE registered_date = ? AND is_debug = 0",
            (d,),
        ).fetchone()["c"]

        dau = facts.execute(
            "SELECT COUNT(DISTINCT f.account_id) AS c FROM fct_message f "
            "JOIN dim_account a ON f.account_id = a.account_id "
            "WHERE f.event_date = ? AND f.direction = 'inbound' AND f.role = 'user' "
            "AND a.is_debug = 0",
            (d,),
        ).fetchone()["c"]

        inbound_messages = facts.execute(
            "SELECT COUNT(*) AS c FROM fct_message f "
            "JOIN dim_account a ON f.account_id = a.account_id "
            "WHERE f.event_date = ? AND f.direction = 'inbound' AND f.role = 'user' "
            "AND a.is_debug = 0",
            (d,),
        ).fetchone()["c"]

        # 首聊 cohort：当日 is_account_first_ever=1 的非 debug 账号。
        cohort = [
            r["account_id"]
            for r in facts.execute(
                "SELECT DISTINCT f.account_id FROM fct_message f "
                "JOIN dim_account a ON f.account_id = a.account_id "
                "WHERE f.event_date = ? AND f.is_account_first_ever = 1 AND a.is_debug = 0",
                (d,),
            ).fetchall()
        ]
        cohort_size = len(cohort)

        retained = 0
        rate: Optional[float] = None
        if next_day >= today:
            # 次日尚未结束，窗口未闭合，不计留存。
            status = "window_open"
        elif cohort_size == 0:
            status = "observe_only"
        else:
            placeholders = ",".join("?" * cohort_size)
            retained = facts.execute(
                "SELECT COUNT(DISTINCT account_id) AS c FROM fct_message "
                "WHERE event_date = ? AND direction = 'inbound' AND role = 'user' "
                f"AND account_id IN ({placeholders})",
                [next_day, *cohort],
            ).fetchone()["c"]
            if cohort_size >= MIN_COHORT:
                rate = round(retained / cohort_size, 4)
                status = "ok"
            else:
                status = "observe_only"

        marts.execute(
            "INSERT OR REPLACE INTO agg_daily_users"
            "(date, new_users, dau, inbound_messages, d1_cohort_size, d1_retained, "
            " d1_retention_rate, d1_status, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d,
                new_users,
                dau,
                inbound_messages,
                cohort_size,
                retained,
                rate,
                status,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        marts.commit()

        return {
            "date": d,
            "new_users": new_users,
            "dau": dau,
            "inbound_messages": inbound_messages,
            "d1_cohort_size": cohort_size,
            "d1_retained": retained,
            "d1_retention_rate": rate,
            "d1_status": status,
        }
    finally:
        facts.close()
        marts.close()
