from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """Return current time as a timezone-aware Beijing (UTC+8) datetime."""
    return datetime.now(BEIJING_TZ)


def beijing_now_str() -> str:
    """Return current Beijing time as a naive string for DB storage: 'YYYY-MM-DD HH:MM:SS'."""
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def beijing_weekday_str(now: datetime) -> str:
    """Return the Chinese weekday label (周一…周日) for *now*."""
    return _WEEKDAY_CN[now.weekday()]


def beijing_daypart_str(now: datetime) -> str:
    """Return a coarse Chinese day-part label for *now*'s hour (Beijing buckets)."""
    h = now.hour
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 17:
        return "下午"
    if 17 <= h < 19:
        return "傍晚"
    if 19 <= h < 23:
        return "晚上"
    return "深夜"
