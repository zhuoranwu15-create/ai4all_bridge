"""dim_account 装载：账号维度当前态（SCD-1 主键 UPSERT）。

注册信息来自 accounts，首聊/活跃日来自 messages 的 role=user 入站聚合。
"""

import sqlite3
from datetime import date
from typing import Optional


def _iso_week_from_date_str(date_str: Optional[str]) -> Optional[str]:
    """把 YYYY-MM-DD 转 ISO 周标签；空值安全。"""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection, now_iso: str) -> int:
    """全量刷新 dim_account 当前态，返回处理账号数。"""
    accounts = source_conn.execute(
        "SELECT id, channel, is_debug, created_at, onboarding_state FROM accounts"
    ).fetchall()

    # 每账号 role=user 入站活跃聚合（首条时间、首聊日、最近活跃日）。
    activity = {}
    for r in source_conn.execute(
        "SELECT account_id, "
        "       MIN(created_at) AS first_inbound_at, "
        "       MIN(DATE(created_at)) AS first_active_date, "
        "       MAX(DATE(created_at)) AS last_active_date "
        "FROM messages "
        "WHERE direction = 'inbound' AND role = 'user' "
        "GROUP BY account_id"
    ).fetchall():
        activity[r["account_id"]] = r

    rows = []
    for a in accounts:
        act = activity.get(a["id"])
        registered_at = a["created_at"]
        registered_date = registered_at[:10] if registered_at else None
        rows.append(
            (
                a["id"],
                a["channel"],
                int(a["is_debug"] or 0),
                registered_at,
                registered_date,
                _iso_week_from_date_str(registered_date),
                act["first_inbound_at"] if act else None,
                act["first_active_date"] if act else None,
                act["last_active_date"] if act else None,
                a["onboarding_state"],
                now_iso,
            )
        )

    facts_conn.executemany(
        "INSERT INTO dim_account"
        "(account_id, channel, is_debug, registered_at, registered_date, registered_week, "
        " first_inbound_at, first_active_date, last_active_date, current_onboarding_state, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        " channel = excluded.channel, "
        " is_debug = excluded.is_debug, "
        " registered_at = excluded.registered_at, "
        " registered_date = excluded.registered_date, "
        " registered_week = excluded.registered_week, "
        " first_inbound_at = excluded.first_inbound_at, "
        " first_active_date = excluded.first_active_date, "
        " last_active_date = excluded.last_active_date, "
        " current_onboarding_state = excluded.current_onboarding_state, "
        " updated_at = excluded.updated_at",
        rows,
    )
    return len(rows)
