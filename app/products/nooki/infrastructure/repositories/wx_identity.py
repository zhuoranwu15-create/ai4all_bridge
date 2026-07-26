"""`nooki_wx_identities` 读写：openid <-> platform_user_id 绑定，唯一约束 `(app_id, openid)`。"""
from __future__ import annotations

from typing import Optional

from app.db._core import _new_id, connect


def find_platform_user_id_by_openid(*, app_id: str, openid: str) -> Optional[str]:
    """按 `(app_id, openid)` 找已绑定的 platform_user_id；未绑定返回 None。"""

    with connect() as conn:
        row = conn.execute(
            "SELECT platform_user_id FROM nooki_wx_identities WHERE app_id = ? AND openid = ?",
            (app_id, openid),
        ).fetchone()
    return str(row["platform_user_id"]) if row is not None else None


def bind_wx_identity(
    *, platform_user_id: str, app_id: str, openid: str, unionid: Optional[str] = None
) -> None:
    """建立 openid 绑定；`(app_id, openid)` 已存在时是幂等 no-op（不改绑到另一个 platform_user）。"""

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO nooki_wx_identities(id, platform_user_id, app_id, openid, unionid)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(app_id, openid) DO NOTHING
            """,
            (_new_id("nkwx"), platform_user_id, app_id, openid, unionid),
        )


__all__ = ["find_platform_user_id_by_openid", "bind_wx_identity"]
