"""`nooki_wx_identities` 读写：openid <-> platform_user_id 绑定，唯一约束 `(wx_appid, openid)`。

阶段3 改造：身份查询从 `(app_id, openid)` 改为 `(wx_appid, openid)`——同一 Nooki 产品
可能对应多个小程序（不同 wx_appid），按微信小程序 appid 隔离更准确。
"""
from __future__ import annotations

from typing import Optional

from app.db._core import _new_id, connect

# app_id 列保留向后兼容但固定为 "nooki"（身份唯一键已改为 wx_appid + openid）
_APP_ID_VALUE = "nooki"


def find_platform_user_id_by_openid(*, wx_appid: str, openid: str) -> Optional[str]:
    """按 `(wx_appid, openid)` 找已绑定的 platform_user_id；未绑定返回 None。"""

    with connect() as conn:
        row = conn.execute(
            "SELECT platform_user_id FROM nooki_wx_identities WHERE wx_appid = ? AND openid = ?",
            (wx_appid, openid),
        ).fetchone()
    return str(row["platform_user_id"]) if row is not None else None


def bind_wx_identity(
    *, platform_user_id: str, wx_appid: str, openid: str, unionid: Optional[str] = None
) -> None:
    """建立 openid 绑定；`(wx_appid, openid)` 已存在时是幂等 no-op（不改绑到另一个 platform_user）。"""

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO nooki_wx_identities(id, platform_user_id, app_id, wx_appid, openid, unionid)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(wx_appid, openid) DO NOTHING
            """,
            (_new_id("nkwx"), platform_user_id, _APP_ID_VALUE, wx_appid, openid, unionid),
        )


__all__ = ["find_platform_user_id_by_openid", "bind_wx_identity"]
