"""app.products.zhaoxi.infrastructure.persistence.campaign_analytics — 营销活码漏斗埋点的只读/只追加分析层。

见 docs/tech_design/campaign_funnel_analytics_technical_design.md §3/§7。

分两层数据：
- S0 曝光：campaign_visits（本模块 record_campaign_visit 写入；账号创建之前的匿名 PV/UV）。
- S1–S5：不新增埋点，从既有表按 campaign_code join account_campaign_attribution 现算：
  注册=account_campaign_attribution、扫码=binding_intents、激活=channel_bindings、
  onboarding=analytics_events('onboarding_state_changed')。

所有查询均以 campaign_code 约束，只出聚合计数、不返回单账号明细。按天分桶统一用
substr(col,1,10)（SQLite/PG 通用，列存北京墙钟裸串 'YYYY-MM-DD HH:MM:SS'）。
"""
import logging
from typing import Any, Dict, List, Optional

from app.db._core import _clean_text, connect
from app.time_utils import beijing_now

logger = logging.getLogger("ai4all")

__all__ = [
    "record_campaign_visit",
    "get_campaign_visit_stats",
    "get_campaign_funnel",
]

# onboarding to_state → 漏斗字段名映射（analytics_events 'onboarding_state_changed'）。
_ONBOARDING_STATE_FIELDS = {
    "step1_sent": "onboarding_step1",
    "step2_sent": "onboarding_step2",
    "complete": "completed",
}

# visitor_token 落库长度上限：正常是 UUID(36)/回退串(~30)，公开端点防超大值胀库。
_MAX_VISITOR_TOKEN_LEN = 64

# by_day 行的全部计数字段（缺失阶段补 0，做 outer-merge）。
_DAY_COUNT_FIELDS = (
    "pv",
    "uv",
    "registered",
    "scanned",
    "activated",
    "onboarding_step1",
    "onboarding_step2",
    "completed",
)


def record_campaign_visit(
    *,
    campaign_code: str,
    visitor_token: Optional[str] = None,
    page: Optional[str] = None,
    referrer: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """记录一次落地页曝光（S0）。visit_date 由服务端按北京日计算，不信前端时间。

    仅写匿名元数据，严禁写用户正文/PII。调用方（beacon 端点）应 fail-open 吞异常。
    """
    cleaned_code = _clean_text(campaign_code)
    if not cleaned_code:
        raise ValueError("campaign_code is required")
    visit_date = beijing_now().date().isoformat()
    # 存储层兜底：visitor_token 进 COUNT(DISTINCT)，公开端点即使漏截也在此封顶，防胀库。
    token = _clean_text(visitor_token) or None
    if token and len(token) > _MAX_VISITOR_TOKEN_LEN:
        token = token[:_MAX_VISITOR_TOKEN_LEN]
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO campaign_visits (
                campaign_code, visitor_token, page, referrer, user_agent, visit_date
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_code,
                token,
                _clean_text(page) or None,
                (referrer or None),
                (user_agent or None),
                visit_date,
            ),
        )


def get_campaign_visit_stats(
    *, campaign_code: str, date_from: str, date_to: str
) -> Dict[str, Any]:
    """S0 曝光 PV/UV：返回 {pv, uv, by_day: [{date, pv, uv}]}（北京日区间闭合）。"""
    cleaned_code = _clean_text(campaign_code)
    if not cleaned_code:
        return {"pv": 0, "uv": 0, "by_day": []}
    with connect() as conn:
        total = conn.execute(
            """
            SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor_token) AS uv
            FROM campaign_visits
            WHERE campaign_code = ? AND visit_date BETWEEN ? AND ?
            """,
            (cleaned_code, date_from, date_to),
        ).fetchone()
        rows = conn.execute(
            """
            SELECT visit_date AS day,
                   COUNT(*) AS pv,
                   COUNT(DISTINCT visitor_token) AS uv
            FROM campaign_visits
            WHERE campaign_code = ? AND visit_date BETWEEN ? AND ?
            GROUP BY visit_date
            ORDER BY day
            """,
            (cleaned_code, date_from, date_to),
        ).fetchall()
    return {
        "pv": int(total["pv"]) if total else 0,
        "uv": int(total["uv"]) if total else 0,
        "by_day": [
            {"date": r["day"], "pv": int(r["pv"]), "uv": int(r["uv"])} for r in rows
        ],
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """转化率；分母为 0 返回 None（不做除零）。"""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def get_campaign_funnel(
    *, campaign_code: str, date_from: str, date_to: str
) -> Dict[str, Any]:
    """S0–S5 计数漏斗 + 转化率（按 campaign_code 聚合，不串联个体）。

    分段执行 §7.7 的 A–E 查询，在 Python 侧按 day 做 outer-merge 成 by_day，
    求和成 totals，最后算 rates。S4/S5 强制人设活码可能跳过 step2_sent，
    故 onboarding_step2 < onboarding_step1 属正常。
    """
    cleaned_code = _clean_text(campaign_code)
    empty = {
        "campaign_code": cleaned_code or campaign_code,
        "range": {"from": date_from, "to": date_to},
        "totals": {field: 0 for field in _DAY_COUNT_FIELDS},
        "rates": {
            "visit_to_register": None,
            "register_to_scan": None,
            "register_to_complete": None,
        },
        "by_day": [],
    }
    if not cleaned_code:
        return empty

    # by_day 累加器：date -> {字段: 计数}
    by_day: Dict[str, Dict[str, int]] = {}

    def _bump(day: str, field: str, value: int) -> None:
        if not day:
            return
        bucket = by_day.setdefault(day, {f: 0 for f in _DAY_COUNT_FIELDS})
        bucket[field] += int(value)

    # S0 曝光（复用 visit_stats 的按天结果）
    visits = get_campaign_visit_stats(
        campaign_code=cleaned_code, date_from=date_from, date_to=date_to
    )
    for row in visits["by_day"]:
        _bump(row["date"], "pv", row["pv"])
        _bump(row["date"], "uv", row["uv"])

    with connect() as conn:
        # S1 注册（account_id 是 PK，COUNT(*) 即去重账号）
        for row in conn.execute(
            """
            SELECT substr(attributed_at, 1, 10) AS day, COUNT(*) AS registered
            FROM account_campaign_attribution
            WHERE campaign_code = ?
              AND substr(attributed_at, 1, 10) BETWEEN ? AND ?
            GROUP BY substr(attributed_at, 1, 10)
            """,
            (cleaned_code, date_from, date_to),
        ).fetchall():
            _bump(row["day"], "registered", row["registered"])

        # S2 扫码（每账号取最早 completed_at，再按天计账号数）
        for row in conn.execute(
            """
            WITH scanned AS (
                SELECT a.account_id, MIN(bi.completed_at) AS scanned_at
                FROM account_campaign_attribution a
                JOIN binding_intents bi ON bi.account_id = a.account_id
                WHERE a.campaign_code = ?
                  AND bi.status = 'completed'
                  AND bi.completed_at IS NOT NULL
                GROUP BY a.account_id
            )
            SELECT substr(scanned_at, 1, 10) AS day, COUNT(*) AS scanned
            FROM scanned
            WHERE substr(scanned_at, 1, 10) BETWEEN ? AND ?
            GROUP BY substr(scanned_at, 1, 10)
            """,
            (cleaned_code, date_from, date_to),
        ).fetchall():
            _bump(row["day"], "scanned", row["scanned"])

        # S3 激活（channel_bindings.first_seen_at，每账号取最早）
        for row in conn.execute(
            """
            WITH activated AS (
                SELECT a.account_id, MIN(cb.first_seen_at) AS activated_at
                FROM account_campaign_attribution a
                JOIN channel_bindings cb ON cb.account_id = a.account_id
                WHERE a.campaign_code = ?
                GROUP BY a.account_id
            )
            SELECT substr(activated_at, 1, 10) AS day, COUNT(*) AS activated
            FROM activated
            WHERE substr(activated_at, 1, 10) BETWEEN ? AND ?
            GROUP BY substr(activated_at, 1, 10)
            """,
            (cleaned_code, date_from, date_to),
        ).fetchall():
            _bump(row["day"], "activated", row["activated"])

        # S4/S5 onboarding 各步到达（analytics_events JOIN attribution，去重账号）
        for row in conn.execute(
            """
            SELECT e.to_state AS to_state,
                   substr(e.event_time, 1, 10) AS day,
                   COUNT(DISTINCT e.account_id) AS accounts
            FROM analytics_events e
            JOIN account_campaign_attribution a ON a.account_id = e.account_id
            WHERE a.campaign_code = ?
              AND e.event_name = 'onboarding_state_changed'
              AND e.to_state IN ('step1_sent', 'step2_sent', 'complete')
              AND substr(e.event_time, 1, 10) BETWEEN ? AND ?
            GROUP BY e.to_state, substr(e.event_time, 1, 10)
            """,
            (cleaned_code, date_from, date_to),
        ).fetchall():
            field = _ONBOARDING_STATE_FIELDS.get(row["to_state"])
            if field:
                _bump(row["day"], field, row["accounts"])

    # 组装 totals + by_day 列表（按日期排序）
    totals = {field: 0 for field in _DAY_COUNT_FIELDS}
    by_day_list: List[Dict[str, Any]] = []
    for day in sorted(by_day):
        bucket = by_day[day]
        for field in _DAY_COUNT_FIELDS:
            totals[field] += bucket[field]
        by_day_list.append({"date": day, **bucket})

    # UV 是集合基数、跨天不可加：totals["uv"] 用区间级 COUNT(DISTINCT)，不能逐日求和
    # （回访用户同一 visitor_token 多天各出现一次会被重复计）。by_day.uv 仍为当天去重值。
    totals["uv"] = int(visits["uv"])

    rates = {
        "visit_to_register": _rate(totals["registered"], totals["uv"]),
        "register_to_scan": _rate(totals["scanned"], totals["registered"]),
        "register_to_complete": _rate(totals["completed"], totals["registered"]),
    }

    return {
        "campaign_code": cleaned_code,
        "range": {"from": date_from, "to": date_to},
        "totals": totals,
        "rates": rates,
        "by_day": by_day_list,
    }
