"""Nooki 用户显式偏好读写：`nooki_user_profile.explicit_preferences`（archetype/companion_name 等）。

只做整字段合并 upsert，不做 schema 校验——校验属于 API 层（如 archetype 枚举值），这里只负责
按 `platform_user_id` 隔离地读/写一个 JSON blob。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from app.db._core import connect
from app.time_utils import beijing_now_str

DEFAULT_ARCHETYPE = "gentle"
DEFAULT_COMPANION_NAME = "我的小动物"


def get_explicit_preferences(platform_user_id: str) -> Dict[str, Any]:
    """返回该用户的显式偏好；无记录或字段为空时返回空 dict。"""

    with connect() as conn:
        row = conn.execute(
            "SELECT explicit_preferences FROM nooki_user_profile WHERE platform_user_id = ?",
            (platform_user_id,),
        ).fetchone()
    if row is None:
        return {}
    raw = dict(row).get("explicit_preferences")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def set_explicit_preferences(platform_user_id: str, **fields: Any) -> Dict[str, Any]:
    """把非 None 的字段合并进已有偏好并整体覆写；返回合并后的完整偏好。"""

    current = get_explicit_preferences(platform_user_id)
    updates = {key: value for key, value in fields.items() if value is not None}
    if not updates:
        return current
    merged = {**current, **updates}
    payload = json.dumps(merged, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO nooki_user_profile(platform_user_id, explicit_preferences, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(platform_user_id) DO UPDATE SET
                explicit_preferences = excluded.explicit_preferences,
                updated_at = excluded.updated_at
            """,
            (platform_user_id, payload, beijing_now_str()),
        )
    return merged


__all__ = [
    "DEFAULT_ARCHETYPE",
    "DEFAULT_COMPANION_NAME",
    "get_explicit_preferences",
    "set_explicit_preferences",
]
