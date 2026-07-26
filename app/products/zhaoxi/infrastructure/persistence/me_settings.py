"""「我的」Tab 的账号级设置数据原语：注销流水（ME-06/07）与通知偏好（ME-10）。

两张表都以 ``platform_user_id`` 为唯一锚。**没有任何一个函数接受 runtime
``account_id``**——「我的」是真人级设置，用 account 代替真人会在 legacy 老用户
（一个真人可能映射多个 runtime account）上直接串号。

本模块只落「注销这件事发生过」的流水行；真正动用户数据的清除逻辑在
:mod:`app.products.zhaoxi.application.account_deletion`，两者分开是为了让「删数据」
只有一个入口、便于审计。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.db._backend import is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "DELETION_REASON_CODES",
    "QUIET_LEVELS",
    "get_notification_preferences",
    "get_last_deletion_record",
    "record_deletion_execution",
    "set_notification_preferences",
]

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


def record_deletion_execution(
    *,
    platform_user_id: str,
    app_id: str,
    reason_code: Optional[str],
    purge_stats: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """记录一次**已完成**的注销清除，返回流水行。

    每次注销都插新行、不做幂等回放：注销后同一手机号可以重新注册，第二次注销是另一次
    独立的清除事件，合并成一行会丢掉追溯链。
    """
    if reason_code is not None and reason_code not in DELETION_REASON_CODES:
        raise ValueError("invalid deletion reason_code")
    current = _db_time(now)
    request_id = _new_id("adr")
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO account_deletion_requests(
                id, platform_user_id, app_id, status, reason_code,
                executed_at, purge_stats_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'executed', ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                platform_user_id,
                app_id,
                reason_code,
                current,
                json.dumps(purge_stats or {}, ensure_ascii=False, sort_keys=True),
                current,
                current,
            ),
        )
        row = conn.execute(
            "SELECT * FROM account_deletion_requests WHERE id = ?", (request_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("deletion record disappeared after insert")
    return dict(row)


def get_last_deletion_record(
    *, platform_user_id: str, app_id: str
) -> Optional[Dict[str, Any]]:
    """读取该真人在本产品下最近一次注销流水；从未注销返回 None。供运营/测试核对。"""
    with _tx(None) as tx:
        row = tx.execute(
            """
            SELECT * FROM account_deletion_requests
            WHERE platform_user_id = ? AND app_id = ?
            ORDER BY executed_at DESC, id DESC
            LIMIT 1
            """,
            (platform_user_id, app_id),
        ).fetchone()
    return dict(row) if row else None


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
