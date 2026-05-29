import calendar
import re
from datetime import datetime, timedelta

_VALID_RECUR_RE = re.compile(
    r"^(daily|weekly:[0-6]|monthly:([1-9]|[12][0-9]|3[01]))$"
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
    """Validate and normalize recur_rule. Raises ValueError on bad input."""
    normalized = value.strip().lower()
    if not _VALID_RECUR_RE.match(normalized):
        raise ValueError(f"Invalid recur_rule: {value!r}")
    return normalized


def compute_next_due_at(recur_rule: str, last_due_at: datetime) -> datetime:
    """Compute the next trigger datetime for a recurring reminder."""
    if recur_rule == "daily":
        return last_due_at + timedelta(days=1)

    if recur_rule.startswith("weekly:"):
        target_weekday = int(recur_rule.split(":")[1])  # 0=Mon, 6=Sun
        days_ahead = target_weekday - last_due_at.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return last_due_at + timedelta(days=days_ahead)

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
