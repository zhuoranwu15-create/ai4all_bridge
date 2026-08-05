"""鸣蝉 Native 用户展示资料的受控取值与结构校验。"""
from __future__ import annotations

from typing import Dict, Optional

USER_AVATAR_KEYS: Dict[str, str] = {
    f"user_{index:02d}": f"/companion_world/avatars/user/user_{index:02d}.png"
    for index in range(1, 9)
}
MAX_NICKNAME_CHARS = 20
MIN_NICKNAME_CHARS = 1

_ALLOWED_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7A3),
)
_ALLOWED_ASCII = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. ·"
)


class MingchanUserProfileError(ValueError):
    """鸣蝉资料受控取值校验失败。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _valid_nickname_character(char: str) -> bool:
    if char in _ALLOWED_ASCII:
        return True
    point = ord(char)
    return any(start <= point <= end for start, end in _ALLOWED_RANGES)


def resolve_user_avatar_ref(avatar_key: str) -> str:
    """把受控头像 key 解析为公开资源引用。"""

    from app.config import settings

    ref = USER_AVATAR_KEYS.get(str(avatar_key or "").strip())
    if ref is None:
        raise MingchanUserProfileError("avatar_key_invalid")
    base = str(
        getattr(settings, "mingchan_asset_base_url", "") or ""
    ).rstrip("/")
    return f"{base}{ref}" if base else ref


def optional_user_avatar_ref(avatar_key: Optional[str]) -> Optional[str]:
    """未设置或遗留未知头像时返回 ``None``。"""

    key = str(avatar_key or "").strip()
    return resolve_user_avatar_ref(key) if key in USER_AVATAR_KEYS else None


def validate_nickname(value: str) -> str:
    """校验昵称长度和客户端安全字符集，返回清理后的值。"""

    cleaned = str(value or "").strip()
    if not (MIN_NICKNAME_CHARS <= len(cleaned) <= MAX_NICKNAME_CHARS):
        raise MingchanUserProfileError("nickname_length_invalid")
    if not all(_valid_nickname_character(char) for char in cleaned):
        raise MingchanUserProfileError("nickname_charset_invalid")
    return cleaned


def profile_options_catalog() -> dict:
    """返回客户端头像选择器和昵称长度约束。"""

    return {
        "avatars": [
            {"key": key, "avatar_ref": resolve_user_avatar_ref(key)}
            for key in USER_AVATAR_KEYS
        ],
        "limits": {
            "nickname_chars": MAX_NICKNAME_CHARS,
            "nickname_min_chars": MIN_NICKNAME_CHARS,
        },
    }


__all__ = [
    "MAX_NICKNAME_CHARS",
    "MIN_NICKNAME_CHARS",
    "MingchanUserProfileError",
    "USER_AVATAR_KEYS",
    "optional_user_avatar_ref",
    "profile_options_catalog",
    "resolve_user_avatar_ref",
    "validate_nickname",
]
