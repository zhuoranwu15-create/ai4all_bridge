import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

BEIJING_TZ = timezone(timedelta(hours=8))

# 本系统当前假设单一北京时区（DB 列、scheduler、日志均按 UTC+8）。
# 海外业务提醒：若未来服务海外用户，必须重新设计为按账号/用户时区处理，
# 并修订本文件所有 beijing_* 工具与下方时区校验。
EXPECTED_HOST_UTC_OFFSET_HOURS = 8


def beijing_now() -> datetime:
    """Return current time as a timezone-aware Beijing (UTC+8) datetime."""
    return datetime.now(BEIJING_TZ)


def beijing_now_str() -> str:
    """Return current Beijing time as a naive string for DB storage: 'YYYY-MM-DD HH:MM:SS'."""
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")


def beijing_naive_now() -> datetime:
    """Return current Beijing (UTC+8) wall-clock as a naive datetime.

    DB timestamp columns store Beijing wall-clock via ``datetime('now','+8 hours')``
    as naive strings. Scheduler / proactive comparisons must use this instead of
    ``datetime.now()`` (which is server-local) so that due-time, window and
    quiet-hour checks line up with stored values regardless of host timezone.
    Naive (not aware) on purpose, to stay arithmetic-compatible with the rest of
    the proactive code and the naive datetimes parsed back from the DB.
    """
    return beijing_now().replace(tzinfo=None)


def host_utc_offset_hours() -> float:
    """Return the host process's current local UTC offset in hours."""
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600.0 if offset is not None else 0.0


def verify_host_timezone(logger: Optional[logging.Logger] = None) -> bool:
    """宿主机时区第二层防御：检测到非 UTC+8 时发 ERROR 报警，但兼容继续运行。

    调度/主动消息已统一用 :func:`beijing_naive_now` 与北京时间 DB 列比较，因此即使
    宿主机不是 Asia/Shanghai，主动消息时序也不会错乱。但系统其它路径（日志、其余
    ``datetime.now()`` 调用方）仍假设宿主机为北京时区。这里检测到偏移异常时记 ERROR
    （经 Feishu 告警 handler 自动上报），并返回 ``False``，但**不阻断启动**。

    海外业务提醒：本校验假设单一北京时区。若未来做海外业务，需要按账号/用户时区
    重新设计，并修订此处与所有 beijing_* 时间工具。
    """
    log = logger or logging.getLogger("ai4all.timezone")
    offset = host_utc_offset_hours()
    if abs(offset - EXPECTED_HOST_UTC_OFFSET_HOURS) < 0.01:
        return True
    log.error(
        "宿主机时区不是 Asia/Shanghai(UTC+8)：host_utc_offset=%+.1fh。"
        "调度已用 beijing_naive_now() 故主动消息时序仍正确，但请设置 TZ=Asia/Shanghai "
        "以对齐日志与其它 datetime.now() 调用方。（若服务海外业务需重新设计单时区假设。）",
        offset,
    )
    return False


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
