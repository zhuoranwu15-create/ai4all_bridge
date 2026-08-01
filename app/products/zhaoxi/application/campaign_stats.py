"""运营与创建者共用的 campaign 聚合统计日期规则。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from app.time_utils import beijing_now

DEFAULT_STATS_WINDOW_DAYS = 14
MAX_STATS_WINDOW_DAYS = 92


def resolve_campaign_stats_range(
    date_from: Optional[str],
    date_to: Optional[str],
    *,
    today: Optional[date] = None,
) -> tuple[str, str]:
    """解析北京自然日统计区间；缺省最近 14 天，跨度最多 92 天。"""
    current = today or beijing_now().date()

    def parse(value: Optional[str], default: date) -> date:
        if value is None:
            return default
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc

    to_date = parse(date_to, current)
    from_date = parse(
        date_from,
        to_date - timedelta(days=DEFAULT_STATS_WINDOW_DAYS - 1),
    )
    if from_date > to_date:
        raise ValueError("from must be <= to")
    if (to_date - from_date).days > MAX_STATS_WINDOW_DAYS:
        raise ValueError(f"range must be <= {MAX_STATS_WINDOW_DAYS} days")
    return from_date.isoformat(), to_date.isoformat()


__all__ = [
    "DEFAULT_STATS_WINDOW_DAYS",
    "MAX_STATS_WINDOW_DAYS",
    "resolve_campaign_stats_range",
]
