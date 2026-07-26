"""真人用户 Profile 的受控取值与校验（ME-01）。

与 `companion_world/persona_catalog.py` 的区别：那份描述的是**AI 居民**的人设，这份描述
的是**真人自己**在 App 里的展示身份。两者的头像库刻意分开——把 AI 角色头像给真人用会
在真人会话里造成身份混淆。

昵称沿用居民展示名的字符白名单（同形攻击与渲染破坏的防线一致），但**必须额外过
`text_sanitizer`**：昵称会展示给来访的真实好友，是对外可见的自由文本，风险面与角色名
相同（D-B）。
"""
from typing import Dict, Optional

from app.products.zhaoxi.domain.companion_world.persona_catalog import (
    is_valid_display_name,
)

# 真人头像。与居民头像同样是「受控 key → 资产相对路径」，客户端不提交 URL，
# 前缀由 `COMPANION_WORLD_ASSET_BASE_URL` 决定。扩充头像库是运营任务（补资产 + 加一行）。
USER_AVATAR_KEYS: Dict[str, str] = {
    "user_01": "/companion_world/avatars/user/user_01.png",
    "user_02": "/companion_world/avatars/user/user_02.png",
    "user_03": "/companion_world/avatars/user/user_03.png",
    "user_04": "/companion_world/avatars/user/user_04.png",
    "user_05": "/companion_world/avatars/user/user_05.png",
    "user_06": "/companion_world/avatars/user/user_06.png",
    "user_07": "/companion_world/avatars/user/user_07.png",
    "user_08": "/companion_world/avatars/user/user_08.png",
}

MAX_NICKNAME_CHARS = 20
MIN_NICKNAME_CHARS = 1


class UserProfileError(ValueError):
    """Profile 受控取值校验失败；由 API 层翻译为稳定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def resolve_user_avatar_ref(avatar_key: str) -> str:
    """把受控头像 key 解析为 avatar_ref；未知 key 直接拒绝，不回落默认头像。"""
    from app.config import settings

    ref = USER_AVATAR_KEYS.get((avatar_key or "").strip())
    if ref is None:
        raise UserProfileError("avatar_key_invalid")
    base = str(getattr(settings, "companion_world_asset_base_url", "") or "").rstrip("/")
    return f"{base}{ref}" if base else ref


def optional_user_avatar_ref(avatar_key: Optional[str]) -> Optional[str]:
    """未设置头像时返回 None，不抛错——历史用户与新用户都没有头像 key。"""
    key = (avatar_key or "").strip()
    return resolve_user_avatar_ref(key) if key in USER_AVATAR_KEYS else None


def validate_nickname(value: str) -> str:
    """校验昵称字符集与长度，返回去掉首尾空白的值。

    只做**结构**校验；语义安全（冒充、辱骂、真人复刻等）由 `text_sanitizer` 负责。
    """
    cleaned = (value or "").strip()
    if not (MIN_NICKNAME_CHARS <= len(cleaned) <= MAX_NICKNAME_CHARS):
        raise UserProfileError("nickname_length_invalid")
    if not is_valid_display_name(cleaned):
        raise UserProfileError("nickname_charset_invalid")
    return cleaned


def profile_options_catalog() -> dict:
    """下发给客户端的受控取值表；客户端据此渲染头像选择器，不硬编码枚举。"""
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
    "USER_AVATAR_KEYS",
    "UserProfileError",
    "optional_user_avatar_ref",
    "profile_options_catalog",
    "resolve_user_avatar_ref",
    "validate_nickname",
]
