"""dim_date 装载：按区间生成日历维度，不读操作库。幂等 INSERT OR IGNORE。"""

import sqlite3
from datetime import date, timedelta


def _iso_week(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def upsert_date_range(facts_conn: sqlite3.Connection, start: date, end: date) -> int:
    """填充 [start, end]（含端点）的日历行；已存在的日期跳过。返回写入尝试行数。"""
    if end < start:
        return 0
    rows = []
    d = start
    while d <= end:
        rows.append(
            (
                d.isoformat(),
                d.year,
                d.month,
                d.day,
                _iso_week(d),
                d.isoweekday(),
                1 if d.isoweekday() >= 6 else 0,
            )
        )
        d += timedelta(days=1)
    facts_conn.executemany(
        "INSERT OR IGNORE INTO dim_date"
        "(date, year, month, day, iso_week, day_of_week, is_weekend) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)
