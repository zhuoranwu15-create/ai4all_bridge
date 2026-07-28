"""分析域4：微信首次聊天 onboarding 漏斗（第②段）。

读 fct_onboarding_journey / dim_account（排除 debug），写 agg_onboarding_funnel_daily。
当前态分布为每日快照（reflect 最近 ETL 状态）；注册 cohort 完成按 registered_date。
人设分布需 onboarding_events 落库后启用（foundation §4.4），此处仅占位。
Web 注册/扫码绑定（第①段）待事件补齐，不在本模块。
"""

import json
from datetime import datetime
from typing import Dict, Optional

from nearline.analytics.facts_db import connect_facts
from nearline.analytics.marts_db import connect_marts, init_marts
from nearline.analytics.metrics._scope import account_clause
from nearline.reporting.scope import ReportScope

_STYLE_PLACEHOLDER = {"_note": "数据待积累：人设选择写在上下文文件，待 onboarding_events 落库后统计"}


def compute(
    target_date: str,
    facts_db_override: Optional[str] = None,
    scope: Optional[ReportScope] = None,
) -> Dict:
    """计算并落库某日 onboarding 漏斗快照。"""
    init_marts()
    facts = connect_facts(facts_db_override)
    marts = connect_marts()
    try:
        d = target_date
        scope_sql, scope_params = (
            account_clause(scope) if scope is not None else ("1 = 1", [])
        )

        # 当前态分布（快照，排除 debug）。
        dist_rows = facts.execute(
            "SELECT j.current_state AS state, COUNT(*) AS c "
            "FROM fct_onboarding_journey j JOIN dim_account a ON j.account_id = a.account_id "
            f"WHERE a.is_debug = 0 AND {scope_sql} GROUP BY j.current_state",
            scope_params,
        ).fetchall()
        dist = {r["state"]: r["c"] for r in dist_rows}

        cnt_complete = dist.get("complete", 0)
        cnt_timed_out = dist.get("timed_out", 0)
        denom = cnt_complete + cnt_timed_out
        completion_rate = round(cnt_complete / denom, 4) if denom else None

        # 注册 cohort（当日注册）及其当前完成数。
        cohort_registered = facts.execute(
            "SELECT COUNT(*) AS c FROM dim_account a WHERE registered_date = ? AND is_debug = 0 "
            f"AND {scope_sql}",
            [d, *scope_params],
        ).fetchone()["c"]
        cohort_completed = facts.execute(
            "SELECT COUNT(*) AS c "
            "FROM dim_account a JOIN fct_onboarding_journey j ON a.account_id = j.account_id "
            "WHERE a.registered_date = ? AND a.is_debug = 0 AND j.current_state = 'complete' "
            f"AND {scope_sql}",
            [d, *scope_params],
        ).fetchone()["c"]

        result = {
            "date": d,
            "cnt_pending": dist.get("pending", 0),
            "cnt_step1_sent": dist.get("step1_sent", 0),
            "cnt_step2_sent": dist.get("step2_sent", 0),
            "cnt_step3_sent": dist.get("step3_sent", 0),
            "cnt_complete": cnt_complete,
            "cnt_timed_out": cnt_timed_out,
            "completion_rate": completion_rate,
            "cohort_registered": cohort_registered,
            "cohort_completed": cohort_completed,
        }

        if scope is None:
            marts.execute(
            "INSERT OR REPLACE INTO agg_onboarding_funnel_daily"
            "(date, cnt_pending, cnt_step1_sent, cnt_step2_sent, cnt_step3_sent, cnt_complete, "
            " cnt_timed_out, completion_rate, cohort_registered, cohort_completed, "
            " style_distribution_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d, result["cnt_pending"], result["cnt_step1_sent"], result["cnt_step2_sent"],
                result["cnt_step3_sent"], cnt_complete, cnt_timed_out, completion_rate,
                cohort_registered, cohort_completed,
                json.dumps(_STYLE_PLACEHOLDER, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
            )
        else:
            marts.execute(
                "INSERT OR REPLACE INTO agg_onboarding_funnel_daily_scoped"
                "(date, app_id, channel, payload_json, computed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    d, scope.app_id, scope.channel,
                    json.dumps(result, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        marts.commit()
        return result
    finally:
        facts.close()
        marts.close()
