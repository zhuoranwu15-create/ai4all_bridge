"""北京自然日 Feed 窗口解析与 slot 判定。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

from app.time_utils import BEIJING_TZ


def _minute_of_day(value: str, *, allow_24: bool) -> int:
    text = str(value or "").strip()
    if allow_24 and text == "24:00":
        return 24 * 60
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except ValueError as err:
        raise ValueError("feed windows must use HH:MM") from err
    return parsed.hour * 60 + parsed.minute


@dataclass(frozen=True)
class FeedSlot:
    """当前北京窗口解析结果。"""

    name: str
    local_date: str
    window_end_at: datetime


@dataclass(frozen=True)
class FeedWindows:
    """两个不重叠半开区间；分钟值允许 end=1440 表示当日 24:00。"""

    morning_start: int
    morning_end: int
    evening_start: int
    evening_end: int

    @classmethod
    def parse(
        cls,
        *,
        morning_start: str,
        morning_end: str,
        evening_start: str,
        evening_end: str,
    ) -> "FeedWindows":
        result = cls(
            morning_start=_minute_of_day(morning_start, allow_24=False),
            morning_end=_minute_of_day(morning_end, allow_24=True),
            evening_start=_minute_of_day(evening_start, allow_24=False),
            evening_end=_minute_of_day(evening_end, allow_24=True),
        )
        if not (
            0 <= result.morning_start < result.morning_end
            <= result.evening_start < result.evening_end <= 24 * 60
        ):
            raise ValueError("feed windows must be ordered, non-overlapping, and same-day")
        return result

    def resolve(self, now: datetime) -> Optional[FeedSlot]:
        """返回 now 所在 slot；窗口外返回 None。"""
        current = now
        if current.tzinfo is not None:
            current = current.astimezone(BEIJING_TZ).replace(tzinfo=None)
        minute = current.hour * 60 + current.minute
        if self.morning_start <= minute < self.morning_end:
            name, end_minute = "morning", self.morning_end
        elif self.evening_start <= minute < self.evening_end:
            name, end_minute = "evening", self.evening_end
        else:
            return None
        day_start = datetime.combine(current.date(), time.min)
        return FeedSlot(
            name=name,
            local_date=current.strftime("%Y-%m-%d"),
            window_end_at=day_start + timedelta(minutes=end_minute),
        )
