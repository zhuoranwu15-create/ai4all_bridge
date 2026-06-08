"""fct_dreaming_run / fct_dreaming_memory_item 装载：watermark 增量 + 未完成行回填。

run_date / duration_sec 由 started_at、completed_at 派生（操作库无现成列）。
token_input/output 可能为空，原样落库，聚合时再做 NULL 安全。

load_runs 分两步：
  A. watermark 增量插入新行；
  B. 对 completed_at IS NULL 的 facts 行重查源，若源已完成则回填字段。
     （解决 ETL 在 run 完成前跑过、行以空 token/duration 落库后永远不更新的问题。）
"""

import sqlite3

from nearline.analytics._timeutil import date_str, parse_dt
from nearline.analytics.facts_db import get_watermark, set_watermark


def _insert_new_runs(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection) -> int:
    """A 段：watermark 增量插入新 dreaming_runs 行。"""
    last_id = get_watermark(facts_conn, "fct_dreaming_run")
    rows = source_conn.execute(
        "SELECT id, account_id, source_type, status, error, started_at, completed_at, "
        "token_input, token_output "
        "FROM dreaming_runs WHERE id > ? ORDER BY id",
        (last_id,),
    ).fetchall()
    if not rows:
        return 0

    out = []
    max_id = last_id
    for r in rows:
        started = parse_dt(r["started_at"])
        completed = parse_dt(r["completed_at"])
        duration = (
            int((completed - started).total_seconds())
            if started and completed and completed >= started
            else None
        )
        out.append(
            (
                r["id"],
                r["account_id"],
                r["source_type"],
                r["status"],
                r["error"],
                r["started_at"],
                r["completed_at"],
                date_str(r["started_at"]),
                duration,
                r["token_input"],
                r["token_output"],
            )
        )
        if r["id"] > max_id:
            max_id = r["id"]

    facts_conn.executemany(
        "INSERT OR IGNORE INTO fct_dreaming_run"
        "(id, account_id, source_type, status, error, started_at, completed_at, "
        " run_date, duration_sec, token_input, token_output) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        out,
    )
    set_watermark(facts_conn, "fct_dreaming_run", max_id)
    return len(out)


def _refresh_incomplete_runs(source_conn: sqlite3.Connection,
                              facts_conn: sqlite3.Connection) -> int:
    """B 段：对 completed_at IS NULL 的 facts 行重查源，有完成信息则回填。

    ETL 可能在 run 完成前跑过，行以空 token/duration/completed_at 落库，INSERT OR IGNORE
    让后续 ETL 永远跳过它。本段绕过 watermark，按 id 集合重查弥补。
    """
    rows = facts_conn.execute(
        "SELECT id FROM fct_dreaming_run WHERE completed_at IS NULL"
    ).fetchall()
    if not rows:
        return 0

    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    now_done = source_conn.execute(
        f"SELECT id, status, error, started_at, completed_at, token_input, token_output "
        f"FROM dreaming_runs WHERE id IN ({placeholders}) AND completed_at IS NOT NULL",
        ids,
    ).fetchall()
    if not now_done:
        return 0

    for src in now_done:
        started = parse_dt(src["started_at"])
        completed = parse_dt(src["completed_at"])
        duration = (
            int((completed - started).total_seconds())
            if started and completed and completed >= started
            else None
        )
        facts_conn.execute(
            "UPDATE fct_dreaming_run SET "
            "status = ?, error = ?, completed_at = ?, duration_sec = ?, "
            "token_input = ?, token_output = ? "
            "WHERE id = ?",
            (src["status"], src["error"], src["completed_at"], duration,
             src["token_input"], src["token_output"], src["id"]),
        )
    return len(now_done)


def load_runs(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection) -> dict:
    """增量装载 dreaming_runs（A + B 两段），返回 {inserted, refreshed}。"""
    inserted = _insert_new_runs(source_conn, facts_conn)
    refreshed = _refresh_incomplete_runs(source_conn, facts_conn)
    return {"inserted": inserted, "refreshed": refreshed}


def load_memory_items(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection) -> int:
    """增量装载 dreaming_memory_items。category 为自由文本噪声，本表只保留结构化维度。"""
    last_id = get_watermark(facts_conn, "fct_dreaming_memory_item")
    rows = source_conn.execute(
        "SELECT id, dreaming_run_id, account_id, operation, apply_status, skip_reason, created_at "
        "FROM dreaming_memory_items WHERE id > ? ORDER BY id",
        (last_id,),
    ).fetchall()
    if not rows:
        return 0

    out = []
    max_id = last_id
    for r in rows:
        out.append(
            (
                r["id"],
                r["dreaming_run_id"],
                r["account_id"],
                r["operation"],
                r["apply_status"],
                r["skip_reason"],
                r["created_at"],
                date_str(r["created_at"]),
            )
        )
        if r["id"] > max_id:
            max_id = r["id"]

    facts_conn.executemany(
        "INSERT OR IGNORE INTO fct_dreaming_memory_item"
        "(id, dreaming_run_id, account_id, operation, apply_status, skip_reason, created_at, event_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        out,
    )
    set_watermark(facts_conn, "fct_dreaming_memory_item", max_id)
    return len(out)
