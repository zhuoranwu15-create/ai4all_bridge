import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple


@dataclass(frozen=True)
class ParsedReminder:
    due_at: datetime
    text: str
    matched_time_text: str

    @property
    def due_at_db(self) -> str:
        return self.due_at.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def due_at_display(self) -> str:
        return self.due_at.strftime("%Y-%m-%d %H:%M")


_TRIGGER_RE = re.compile(r"(记得)?(帮我)?(到时候)?提醒我|提醒一下我")
_FULL_DATE_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|-|/)\s*(?P<month>\d{1,2})\s*(?:月|-|/)\s*(?P<day>\d{1,2})\s*(?:日|号)?"
)
_MONTH_DAY_RE = re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*(?:日|号)?")
_RELATIVE_DAY_RE = re.compile(r"大后天|后天|明天|今天|今晚")
_COLON_TIME_RE = re.compile(
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚)?\s*"
    r"(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{1,2})"
)
_HOUR_TIME_RE = re.compile(
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚)?\s*"
    r"(?P<hour>\d{1,2})\s*(?:点|时)\s*(?P<half>半)?\s*(?:(?P<minute>\d{1,2})\s*分?)?"
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _resolve_date(text: str, now: datetime) -> Optional[Tuple[datetime, Tuple[int, int]]]:
    full = _FULL_DATE_RE.search(text)
    if full:
        year = int(full.group("year"))
        month = int(full.group("month"))
        day = int(full.group("day"))
        try:
            return datetime(year, month, day), full.span()
        except ValueError:
            return None

    month_day = _MONTH_DAY_RE.search(text)
    if month_day:
        month = int(month_day.group("month"))
        day = int(month_day.group("day"))
        try:
            candidate = datetime(now.year, month, day)
        except ValueError:
            return None
        if candidate.date() < now.date():
            try:
                candidate = datetime(now.year + 1, month, day)
            except ValueError:
                return None
        return candidate, month_day.span()

    relative = _RELATIVE_DAY_RE.search(text)
    if relative:
        token = relative.group(0)
        days = {
            "今天": 0,
            "今晚": 0,
            "明天": 1,
            "后天": 2,
            "大后天": 3,
        }[token]
        return datetime.combine((now + timedelta(days=days)).date(), datetime.min.time()), relative.span()

    return None


def _apply_period(hour: int, period: Optional[str]) -> Optional[int]:
    if hour < 0 or hour > 23:
        return None
    if not period:
        # Avoid ambiguous "明天 3 点" / "明天 10 点". Accept only clear 24h hours.
        if hour == 0 or hour >= 13:
            return hour
        return None
    if period in {"凌晨"}:
        return 0 if hour == 12 else hour
    if period in {"早上", "早晨", "上午"}:
        return hour
    if period == "中午":
        return hour if hour == 12 else hour + 12 if 1 <= hour <= 2 else hour
    if period in {"下午", "傍晚", "晚上", "今晚"}:
        return hour if hour >= 12 else hour + 12
    return None


def _resolve_time(text: str) -> Optional[Tuple[int, int, Tuple[int, int]]]:
    colon = _COLON_TIME_RE.search(text)
    if colon:
        hour = int(colon.group("hour"))
        minute = int(colon.group("minute"))
        if minute < 0 or minute > 59:
            return None
        period = colon.group("period")
        resolved_hour = _apply_period(hour, period) if period else hour
        if resolved_hour is None or resolved_hour < 0 or resolved_hour > 23:
            return None
        return resolved_hour, minute, colon.span()

    hour_match = _HOUR_TIME_RE.search(text)
    if not hour_match:
        return None
    hour = int(hour_match.group("hour"))
    period = hour_match.group("period")
    resolved_hour = _apply_period(hour, period)
    if resolved_hour is None or resolved_hour > 23:
        return None
    minute = 30 if hour_match.group("half") else int(hour_match.group("minute") or 0)
    if minute < 0 or minute > 59:
        return None
    return resolved_hour, minute, hour_match.span()


def _remove_span(text: str, span: Tuple[int, int]) -> str:
    return text[: span[0]] + " " + text[span[1] :]


def _extract_task_text(text: str, spans: Tuple[Tuple[int, int], ...]) -> str:
    cleaned = text
    for span in sorted(spans, key=lambda item: item[0], reverse=True):
        cleaned = _remove_span(cleaned, span)
    cleaned = _TRIGGER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[，,。.!！?？；;]", " ", cleaned)
    cleaned = re.sub(r"\b(请|帮我|记得|到时候|一下|在|于)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_explicit_reminder(text: str, *, now: Optional[datetime] = None) -> Optional[ParsedReminder]:
    normalized = _normalize_text(text)
    if not normalized or not looks_like_reminder_request(normalized):
        return None

    current = now or datetime.now()
    date_result = _resolve_date(normalized, current)
    time_result = _resolve_time(normalized)
    if date_result is None or time_result is None:
        return None

    date_value, date_span = date_result
    hour, minute, time_span = time_result
    due_at = date_value.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due_at <= current:
        return None

    task_text = _extract_task_text(normalized, (date_span, time_span))
    if not task_text:
        return None

    start = min(date_span[0], time_span[0])
    end = max(date_span[1], time_span[1])
    return ParsedReminder(
        due_at=due_at,
        text=task_text,
        matched_time_text=normalized[start:end].strip(),
    )


def looks_like_reminder_request(text: str) -> bool:
    return bool(_TRIGGER_RE.search(_normalize_text(text)))
