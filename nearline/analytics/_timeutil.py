"""时间解析辅助：统一处理操作库里 'YYYY-MM-DD HH:MM:SS' / ISO 'T' 两种格式。"""

from datetime import datetime
from typing import Optional


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """宽松解析时间戳为 naive datetime（北京本地）；无法解析返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        # fromisoformat（py3.11）同时接受空格与 'T' 分隔；截掉可能的时区/微秒尾巴前先试原值。
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def date_str(value: Optional[str]) -> Optional[str]:
    """取时间戳的日期部分 YYYY-MM-DD；空值安全。"""
    if not value:
        return None
    return str(value)[:10]


def hour_of(value: Optional[str]) -> Optional[int]:
    """取时间戳的小时（0-23）；解析失败返回 None。"""
    dt = parse_dt(value)
    return dt.hour if dt else None
