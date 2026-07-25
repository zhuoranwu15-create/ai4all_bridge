import calendar
import re
from datetime import datetime, time, timedelta

# weekly 支持单个或逗号分隔的多个星期几（0=周一…6=周日），如 weekly:0,2,4 表「一三五」。
# 单值 weekly:6 仍合法（向后兼容）。
_VALID_RECUR_RE = re.compile(
    r"^(daily|weekly:[0-6](,[0-6])*|monthly:([1-9]|[12][0-9]|3[01]))$"
)


def validate_due_at(value: str) -> datetime:
    """Parse and validate LLM-provided due_at. Raises ValueError on bad input."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid due_at format: {value!r}")


def validate_recur_rule(value: str) -> str:
    """Validate and normalize recur_rule. Raises ValueError on bad input.

    weekly 允许多星期几：weekly:0,2,4（去重并按升序归一，便于比较/存储）。
    """
    normalized = value.strip().lower()
    if not _VALID_RECUR_RE.match(normalized):
        raise ValueError(f"Invalid recur_rule: {value!r}")
    if normalized.startswith("weekly:"):
        days = sorted({int(part) for part in normalized.split(":", 1)[1].split(",")})
        normalized = "weekly:" + ",".join(str(d) for d in days)
    return normalized


def compute_next_due_at(recur_rule: str, last_due_at: datetime) -> datetime:
    """Compute the next trigger datetime for a recurring reminder."""
    if recur_rule == "daily":
        return last_due_at + timedelta(days=1)

    if recur_rule.startswith("weekly:"):
        # 支持多星期几：取「严格晚于 last_due 的最近命中日」，命中时刻沿用 last_due 的 HH:MM:SS。
        target_weekdays = sorted(
            {int(part) for part in recur_rule.split(":", 1)[1].split(",")}
        )  # 0=Mon, 6=Sun
        best_ahead = min(
            ((wd - last_due_at.weekday()) % 7) or 7 for wd in target_weekdays
        )
        return last_due_at + timedelta(days=best_ahead)

    if recur_rule.startswith("monthly:"):
        target_day = int(recur_rule.split(":")[1])
        month = last_due_at.month + 1
        year = last_due_at.year
        if month > 12:
            month = 1
            year += 1
        max_day = calendar.monthrange(year, month)[1]
        day = min(target_day, max_day)
        return last_due_at.replace(year=year, month=month, day=day)

    raise ValueError(f"Unknown recur_rule: {recur_rule!r}")


def compute_first_due_at(recur_rule: str, at_time: time, now: datetime) -> datetime:
    """由后端计算周期任务的首次触发时刻（绝不采用 LLM 提供的日期）。

    只信任 LLM 给的「时刻」(at_time)与周期规则(recur_rule)，日期一律按 now 重算：
    取「严格晚于 now、且命中周期规则」的最近一次。

    仅支持 daily / weekly（含多星期几），覆盖动态提醒 v1 需要的形态；monthly 暂不经此
    helper（固定提醒仍由调用方直接给 due_at）。
    """
    anchor = now.replace(
        hour=at_time.hour,
        minute=at_time.minute,
        second=at_time.second,
        microsecond=0,
    )
    if recur_rule == "daily":
        return anchor if anchor > now else anchor + timedelta(days=1)
    if recur_rule.startswith("weekly:"):
        weekdays = {int(part) for part in recur_rule.split(":", 1)[1].split(",")}
        if anchor.weekday() in weekdays and anchor > now:
            return anchor
        return compute_next_due_at(recur_rule, anchor)
    raise ValueError(f"compute_first_due_at unsupported recur_rule: {recur_rule!r}")
