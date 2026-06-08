"""fct_onboarding_journey 装载：账号 onboarding 旅程累积快照。

操作库只有 onboarding_state + onboarding_updated_at，无逐步历史。本表每次 ETL：
  - registered_at 取 accounts.created_at；
  - 当前态对应的里程碑时间戳用 onboarding_updated_at 捕捉；
  - UPSERT 时对 step1/2/3/completed/timed_out 用 COALESCE(已有, 新)保留首次捕捉值，
    使历史里程碑不被后续状态覆盖（accumulating snapshot 语义）。
历史中间里程碑（首次上线前已越过的步骤）无从还原，留空。
"""

import sqlite3
from typing import Optional

from nearline.analytics._timeutil import parse_dt

# onboarding_state -> 该状态对应回填哪个里程碑列
_STATE_MILESTONE = {
    "step1_sent": "step1_sent_at",
    "step2_sent": "step2_sent_at",
    "step3_sent": "step3_sent_at",
    "complete": "completed_at",
    "timed_out": "timed_out_at",
}


def load(source_conn: sqlite3.Connection, facts_conn: sqlite3.Connection, now_iso: str) -> int:
    """全量刷新 onboarding 旅程快照，返回处理账号数。"""
    accounts = source_conn.execute(
        "SELECT id, created_at, onboarding_state, onboarding_updated_at FROM accounts"
    ).fetchall()

    rows = []
    for a in accounts:
        state = a["onboarding_state"]
        updated = a["onboarding_updated_at"]
        milestone_col = _STATE_MILESTONE.get(state or "")
        # 各里程碑候选值：仅当前态对应列拿到 updated，其余为 None（由 COALESCE 保留旧值）。
        step1 = updated if milestone_col == "step1_sent_at" else None
        step2 = updated if milestone_col == "step2_sent_at" else None
        step3 = updated if milestone_col == "step3_sent_at" else None
        completed = updated if milestone_col == "completed_at" else None
        timed_out = updated if milestone_col == "timed_out_at" else None

        is_complete = 1 if state == "complete" else 0
        is_timed_out = 1 if state == "timed_out" else 0

        reg_dt = parse_dt(a["created_at"])
        comp_dt = parse_dt(completed)
        ttc = (
            int((comp_dt - reg_dt).total_seconds())
            if is_complete and reg_dt and comp_dt and comp_dt >= reg_dt
            else None
        )

        rows.append(
            (
                a["id"], a["created_at"], step1, step2, step3, completed, timed_out,
                state, ttc, is_complete, is_timed_out, now_iso,
            )
        )

    facts_conn.executemany(
        "INSERT INTO fct_onboarding_journey"
        "(account_id, registered_at, step1_sent_at, step2_sent_at, step3_sent_at, "
        " completed_at, timed_out_at, current_state, time_to_complete_sec, "
        " is_complete, is_timed_out, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET "
        " registered_at = COALESCE(fct_onboarding_journey.registered_at, excluded.registered_at), "
        " step1_sent_at = COALESCE(fct_onboarding_journey.step1_sent_at, excluded.step1_sent_at), "
        " step2_sent_at = COALESCE(fct_onboarding_journey.step2_sent_at, excluded.step2_sent_at), "
        " step3_sent_at = COALESCE(fct_onboarding_journey.step3_sent_at, excluded.step3_sent_at), "
        " completed_at = COALESCE(fct_onboarding_journey.completed_at, excluded.completed_at), "
        " timed_out_at = COALESCE(fct_onboarding_journey.timed_out_at, excluded.timed_out_at), "
        " current_state = excluded.current_state, "
        " time_to_complete_sec = COALESCE(fct_onboarding_journey.time_to_complete_sec, excluded.time_to_complete_sec), "
        " is_complete = excluded.is_complete, "
        " is_timed_out = excluded.is_timed_out, "
        " updated_at = excluded.updated_at",
        rows,
    )
    return len(rows)
