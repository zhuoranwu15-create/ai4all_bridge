"""fct_message 装载：会话消息事实，按 messages.id watermark 增量、只追加。

is_account_first_ever / is_account_first_of_day 仅对 role=user 入站消息有意义，
用对操作库的相关子查询判定——与已装载量无关，因此增量重跑结果稳定（首条标记单调）。
"""

import sqlite3

from nearline.analytics.facts_db import get_watermark, set_watermark

# 增量取数 + 首条标记（对源 messages 自相关，watermark 安全）。
_SELECT_SQL = """
SELECT
    m.id,
    m.account_id,
    m.session_id,
    m.direction,
    m.role,
    m.message_type,
    m.created_at,
    s.business_day AS business_day,
    CASE WHEN m.direction = 'inbound' AND m.role = 'user' AND NOT EXISTS (
        SELECT 1 FROM messages e
        WHERE e.account_id = m.account_id
          AND e.direction = 'inbound' AND e.role = 'user'
          AND (e.created_at < m.created_at OR (e.created_at = m.created_at AND e.id < m.id))
    ) THEN 1 ELSE 0 END AS is_first_ever,
    CASE WHEN m.direction = 'inbound' AND m.role = 'user' AND NOT EXISTS (
        SELECT 1 FROM messages e
        WHERE e.account_id = m.account_id
          AND e.direction = 'inbound' AND e.role = 'user'
          AND DATE(e.created_at) = DATE(m.created_at)
          AND (e.created_at < m.created_at OR (e.created_at = m.created_at AND e.id < m.id))
    ) THEN 1 ELSE 0 END AS is_first_of_day
FROM messages m
LEFT JOIN sessions s ON m.session_id = s.id
WHERE m.id > ?
ORDER BY m.id
"""


def load(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection) -> int:
    """增量装载新消息，更新 watermark，返回新增行数。"""
    last_id = get_watermark(facts_conn, "fct_message")
    rows = source_conn.execute(_SELECT_SQL, (last_id,)).fetchall()
    if not rows:
        return 0

    out = []
    max_id = last_id
    for r in rows:
        created = r["created_at"]
        event_date = created[:10] if created else None
        event_hour = None
        if created and len(created) >= 13:
            try:
                event_hour = int(created[11:13])
            except ValueError:
                event_hour = None
        out.append(
            (
                r["id"],
                r["account_id"],
                r["session_id"],
                r["direction"],
                r["role"],
                r["message_type"],
                created,
                event_date,
                event_hour,
                r["business_day"],
                r["is_first_ever"],
                r["is_first_of_day"],
            )
        )
        if r["id"] > max_id:
            max_id = r["id"]

    facts_conn.executemany(
        "INSERT OR IGNORE INTO fct_message"
        "(message_pk, account_id, session_id, direction, role, message_type, "
        " created_at, event_date, event_hour, business_day, "
        " is_account_first_ever, is_account_first_of_day) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        out,
    )
    set_watermark(facts_conn, "fct_message", max_id)
    return len(out)
