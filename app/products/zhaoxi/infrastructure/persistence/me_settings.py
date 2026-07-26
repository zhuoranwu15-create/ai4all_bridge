"""「我的」Tab 的账号级设置数据原语：注销申请（ME-06/07）与通知偏好（ME-10）。

两张表都以 ``platform_user_id`` 为唯一锚。**没有任何一个函数接受 runtime
``account_id``**——「我的」是真人级设置，用 account 代替真人会在 legacy 老用户
（一个真人可能映射多个 runtime account）上直接串号。

注销申请刻意只写意图与状态：``mark_due`` 之后停在 ``due``，实际数据清除由运营执行
（见 App PRD ME-07）。本模块不提供任何删除用户数据的函数。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "DELETION_COOLING_DAYS",
    "cancel_deletion_request",
    "get_notification_preferences",
    "get_open_deletion_request",
    "mark_due_deletion_requests",
    "open_deletion_request",
    "set_notification_preferences",
]

# 冷静期：提交注销后 7 天内可自助撤销。与 world visit 的 7 天 pending 取同一量级，
# 便于文案统一；真正的口径以法务结论为准（见 plan §4.2 待拍板）。
DELETION_COOLING_DAYS = 7

DELETION_REASON_CODES = (
    "not_useful",
    "privacy_concern",
    "too_expensive",
    "switching",
    "other",
)

QUIET_LEVELS = ("standard", "quiet")
DEFAULT_QUIET_LEVEL = "standard"

_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _db_time(value: datetime) -> str:
    """统一转成库内裸北京时间字符串，与其余 world 表一致。"""
    return value.strftime(_DB_TIME_FORMAT)


def get_open_deletion_request(
    *, platform_user_id: str, app_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """读取当前未终态（pending/due）的注销申请；没有则 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM account_deletion_requests
            WHERE platform_user_id = ? AND app_id = ?
              AND status IN ('pending', 'due')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (platform_user_id, app_id),
        ).fetchone()
    return dict(row) if row else None


def open_deletion_request(
    *,
    platform_user_id: str,
    app_id: str,
    reason_code: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """幂等提交注销申请：已有未终态申请时**回放原申请**而不是刷新冷静期。

    否则用户可以靠反复点击无限延后 ``effective_at``，也会让运营看到抖动的到期时间。
    """
    if reason_code is not None and reason_code not in DELETION_REASON_CODES:
        raise ValueError("invalid deletion reason_code")
    current = _db_time(now)
    effective_at = _db_time(now + timedelta(days=DELETION_COOLING_DAYS))
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        existing = get_open_deletion_request(
            platform_user_id=platform_user_id, app_id=app_id, conn=conn
        )
        if existing is not None:
            return existing
        request_id = _new_id("adr")
        conn.execute(
            """
            INSERT INTO account_deletion_requests(
                id, platform_user_id, app_id, status, reason_code,
                effective_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                request_id,
                platform_user_id,
                app_id,
                reason_code,
                effective_at,
                current,
                current,
            ),
        )
        row = conn.execute(
            "SELECT * FROM account_deletion_requests WHERE id = ?", (request_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("deletion request disappeared after insert")
    return dict(row)


def cancel_deletion_request(
    *, platform_user_id: str, app_id: str, now: datetime
) -> Optional[Dict[str, Any]]:
    """撤销当前未终态申请；没有可撤销的申请返回 None（调用方翻译为 not_found）。

    ``due`` 同样可撤销：冷静期到了但运营还没执行时，用户仍然有权反悔。一旦
    ``executed`` 就不再可撤销——数据已经清了，撤销没有意义。
    """
    current = _db_time(now)
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        existing = get_open_deletion_request(
            platform_user_id=platform_user_id, app_id=app_id, conn=conn
        )
        if existing is None:
            return None
        conn.execute(
            """
            UPDATE account_deletion_requests
            SET status = 'cancelled', cancelled_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'due')
            """,
            (current, current, existing["id"]),
        )
        row = conn.execute(
            "SELECT * FROM account_deletion_requests WHERE id = ?", (existing["id"],)
        ).fetchone()
    return dict(row) if row else None


def mark_due_deletion_requests(*, now: datetime, limit: int = 200) -> int:
    """把冷静期已过的 ``pending`` 推进到 ``due``，返回推进条数。

    只改状态、不碰任何业务数据；``due`` 是给运营的待办信号。供后续 admin/job 调用，
    App 端不暴露。
    """
    current = _db_time(now)
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM account_deletion_requests
            WHERE status = 'pending' AND effective_at <= ?
            ORDER BY effective_at ASC, id ASC
            LIMIT ?
            """,
            (current, limit),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        for request_id in ids:
            conn.execute(
                """
                UPDATE account_deletion_requests
                SET status = 'due', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (current, request_id),
            )
    return len(ids)


def get_notification_preferences(*, platform_user_id: str) -> Dict[str, Any]:
    """读取通知偏好；缺行返回默认值，不写库（读不产生副作用）。"""
    with _tx(None) as tx:
        row = tx.execute(
            "SELECT quiet_level FROM app_notification_preferences WHERE platform_user_id = ?",
            (platform_user_id,),
        ).fetchone()
    level = str(row["quiet_level"]) if row else DEFAULT_QUIET_LEVEL
    if level not in QUIET_LEVELS:
        level = DEFAULT_QUIET_LEVEL
    return {"quiet_level": level}


def set_notification_preferences(
    *, platform_user_id: str, quiet_level: str, now: datetime
) -> Dict[str, Any]:
    """幂等 upsert 通知偏好。"""
    if quiet_level not in QUIET_LEVELS:
        raise ValueError("invalid quiet_level")
    current = _db_time(now)
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE app_notification_preferences
            SET quiet_level = ?, updated_at = ?
            WHERE platform_user_id = ?
            """,
            (quiet_level, current, platform_user_id),
        )
        if int(cursor.rowcount or 0) <= 0:
            conn.execute(
                """
                INSERT INTO app_notification_preferences(
                    platform_user_id, quiet_level, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (platform_user_id, quiet_level, current, current),
            )
    return {"quiet_level": quiet_level}
