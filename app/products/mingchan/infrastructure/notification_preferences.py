"""鸣蝉产品级通知偏好持久化。"""
from __future__ import annotations

from datetime import datetime
from typing import Dict

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._backend import is_postgres
from app.db._core import _tx, connect

QUIET_LEVELS = ("standard", "quiet")
DEFAULT_QUIET_LEVEL = "standard"


def get_notification_preferences(*, platform_user_id: str) -> Dict[str, str]:
    """读取鸣蝉通知偏好；缺行返回默认值且不产生写入。"""

    with _tx(None) as tx:
        row = tx.execute(
            "SELECT quiet_level FROM product_notification_preferences "
            "WHERE platform_user_id = ? AND app_id = ?",
            (platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    level = str(row["quiet_level"]) if row else DEFAULT_QUIET_LEVEL
    if level not in QUIET_LEVELS:
        level = DEFAULT_QUIET_LEVEL
    return {"quiet_level": level}


def set_notification_preferences(
    *, platform_user_id: str, quiet_level: str, now: datetime
) -> Dict[str, str]:
    """幂等写入鸣蝉通知偏好，不触碰同一真人的其他产品设置。"""

    if quiet_level not in QUIET_LEVELS:
        raise ValueError("invalid quiet_level")
    current = now.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        if not is_postgres():
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO product_notification_preferences(
                platform_user_id, app_id, quiet_level, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform_user_id, app_id) DO UPDATE
            SET quiet_level = excluded.quiet_level,
                updated_at = excluded.updated_at
            """,
            (platform_user_id, MINGCHAN_APP_ID, quiet_level, current, current),
        )
    return {"quiet_level": quiet_level}


__all__ = [
    "DEFAULT_QUIET_LEVEL",
    "QUIET_LEVELS",
    "get_notification_preferences",
    "set_notification_preferences",
]
