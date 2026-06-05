from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """Return current time as a timezone-aware Beijing (UTC+8) datetime."""
    return datetime.now(BEIJING_TZ)


def beijing_now_str() -> str:
    """Return current Beijing time as a naive string for DB storage: 'YYYY-MM-DD HH:MM:SS'."""
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")
