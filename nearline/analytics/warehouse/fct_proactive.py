"""fct_proactive_message 装载：主动外发事实 + 回复晚到归因。

三段装载（foundation §4.2 / ANALYTICS_PLAN §3.6）：
  A. 新 outbound 行先落库——sent 行 resolution_status='pending'，非 sent 行 'not_applicable'。
  C. 对 status != 'sent' 的 facts 行，重查源：若已变为 sent，回填字段并置 resolution_status='pending'。
     （解决 ETL 在发送前跑过、行以旧状态 INSERT OR IGNORE 后永远不更新的问题。）
  B. 每次 ETL 对仍 pending 的 sent 行重算归因：窗口已闭合（或已找到回复）则回填并置 'resolved'。

归因窗口默认 24h，并被同账号下一条 sent 主动消息的 sent_at 截断；归因目标为窗口内
首条 role=user 入站消息。窗口假设存入 reply_window_hours 列，便于后续敏感性分析。
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from nearline.analytics._timeutil import date_str, hour_of, parse_dt
from nearline.analytics.facts_db import get_watermark, set_watermark

DEFAULT_REPLY_WINDOW_HOURS = 24
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _insert_new(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection,
                window_hours: int) -> int:
    """A 段：增量插入新 outbound 行。"""
    last_id = get_watermark(facts_conn, "fct_proactive_message")
    rows = source_conn.execute(
        "SELECT id, account_id, channel, product_category, source, status, policy_reason, "
        "created_at, scheduled_at, sent_at FROM outbound_messages WHERE id > ? ORDER BY id",
        (last_id,),
    ).fetchall()
    if not rows:
        return 0

    out = []
    max_id = last_id
    for r in rows:
        is_sent = r["status"] == "sent"
        resolution = "pending" if is_sent else "not_applicable"
        out.append(
            (
                r["id"],
                r["account_id"],
                r["channel"],
                r["product_category"],
                r["source"],
                r["status"],
                r["policy_reason"],
                r["created_at"],
                date_str(r["created_at"]),
                r["scheduled_at"],
                r["sent_at"],
                date_str(r["sent_at"]),
                hour_of(r["sent_at"]),
                window_hours if is_sent else None,
                resolution,
            )
        )
        if r["id"] > max_id:
            max_id = r["id"]

    facts_conn.executemany(
        "INSERT OR IGNORE INTO fct_proactive_message"
        "(id, account_id, channel, category, source, status, policy_reason, created_at, created_date, "
        " scheduled_at, sent_at, sent_date, sent_hour, reply_window_hours, resolution_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        out,
    )
    set_watermark(facts_conn, "fct_proactive_message", max_id)
    return len(out)


def _refresh_missing_channels(
    source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection
) -> int:
    """从 outbound_messages 回填历史主动消息的事件渠道。"""
    ids = [
        r["id"]
        for r in facts_conn.execute(
            "SELECT id FROM fct_proactive_message WHERE channel IS NULL"
        ).fetchall()
    ]
    updated = 0
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        placeholders = ",".join("?" * len(batch))
        rows = source_conn.execute(
            f"SELECT id, channel FROM outbound_messages WHERE id IN ({placeholders})",
            batch,
        ).fetchall()
        for row in rows:
            if row["channel"]:
                facts_conn.execute(
                    "UPDATE fct_proactive_message SET channel = ? WHERE id = ?",
                    (row["channel"], row["id"]),
                )
                updated += 1
    return updated


def _refresh_unsent(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection,
                   window_hours: int) -> int:
    """C 段：对 facts 中 status != 'sent' 的行，重查源；若源已 sent 则回填字段。

    ETL 可能在消息发送前跑过，此时行以 pending/not_applicable 落库，INSERT OR IGNORE 让后续
    ETL 永远跳过它。本段绕过 watermark，直接按 id 集合重查，弥补这一差距。
    """
    rows = facts_conn.execute(
        "SELECT id FROM fct_proactive_message WHERE status != 'sent'"
    ).fetchall()
    if not rows:
        return 0

    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    now_sent = source_conn.execute(
        f"SELECT id, sent_at FROM outbound_messages "
        f"WHERE id IN ({placeholders}) AND status = 'sent'",
        ids,
    ).fetchall()
    if not now_sent:
        return 0

    for src in now_sent:
        facts_conn.execute(
            "UPDATE fct_proactive_message SET "
            "status = 'sent', sent_at = ?, sent_date = ?, sent_hour = ?, "
            "reply_window_hours = ?, resolution_status = 'pending' "
            "WHERE id = ?",
            (src["sent_at"], date_str(src["sent_at"]), hour_of(src["sent_at"]),
             window_hours, src["id"]),
        )
    return len(now_sent)


def _attribute_pending(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection,
                       now: datetime) -> int:
    """B 段：对仍 pending 的 sent 行做回复归因，回填并在窗口闭合时置 resolved。返回已 resolve 行数。"""
    pending = facts_conn.execute(
        "SELECT id, account_id, channel, sent_at, reply_window_hours FROM fct_proactive_message "
        "WHERE resolution_status = 'pending' AND sent_at IS NOT NULL"
    ).fetchall()
    if not pending:
        return 0

    resolved = 0
    for row in pending:
        sent_dt = parse_dt(row["sent_at"])
        if sent_dt is None:
            continue
        window_hours = int(row["reply_window_hours"] or DEFAULT_REPLY_WINDOW_HOURS)
        window_end = sent_dt + timedelta(hours=window_hours)

        # 同账号下一条 sent 主动消息截断窗口。
        nxt = source_conn.execute(
            "SELECT MIN(sent_at) AS s FROM outbound_messages "
            "WHERE account_id = ? AND status = 'sent' AND sent_at > ? "
            "AND (? IS NULL OR channel = ?)",
            (row["account_id"], row["sent_at"], row["channel"], row["channel"]),
        ).fetchone()
        nxt_dt = parse_dt(nxt["s"]) if nxt else None
        if nxt_dt and nxt_dt < window_end:
            window_end = nxt_dt

        window_end_str = window_end.strftime(_TS_FMT)
        # 窗口内首条 role=user 入站消息（created_at 同为固定格式，可按字符串比较）。
        reply = source_conn.execute(
            "SELECT id, created_at FROM messages "
            "WHERE account_id = ? AND direction = 'inbound' AND role = 'user' "
            "AND created_at > ? AND created_at <= ? "
            "AND (? IS NULL OR (json_valid(raw_json) "
            "AND json_extract(raw_json, '$.channel') = ?)) "
            "ORDER BY created_at, id LIMIT 1",
            (
                row["account_id"], row["sent_at"], window_end_str,
                row["channel"], row["channel"],
            ),
        ).fetchone()

        if reply:
            reply_dt = parse_dt(reply["created_at"])
            latency = int((reply_dt - sent_dt).total_seconds()) if reply_dt else None
            facts_conn.execute(
                "UPDATE fct_proactive_message SET replied = 1, reply_message_id = ?, "
                "reply_latency_sec = ?, resolution_status = 'resolved' WHERE id = ?",
                (reply["id"], latency, row["id"]),
            )
            resolved += 1
        elif window_end <= now:
            # 窗口已闭合且无回复，定论为未回复。
            facts_conn.execute(
                "UPDATE fct_proactive_message SET replied = 0, resolution_status = 'resolved' "
                "WHERE id = ?",
                (row["id"],),
            )
            resolved += 1
        # 否则窗口未闭合，保持 pending，待下次 ETL。
    return resolved


def load(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection,
         now: Optional[datetime] = None,
         window_hours: int = DEFAULT_REPLY_WINDOW_HOURS) -> dict:
    """跑三段装载，返回 {inserted, refreshed, resolved}。"""
    now = now or datetime.now()
    inserted = _insert_new(source_conn, facts_conn, window_hours)
    _refresh_missing_channels(source_conn, facts_conn)
    refreshed = _refresh_unsent(source_conn, facts_conn, window_hours)
    resolved = _attribute_pending(source_conn, facts_conn, now)
    return {"inserted": inserted, "refreshed": refreshed, "resolved": resolved}
