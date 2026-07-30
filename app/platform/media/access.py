"""媒体读 URL 的签名与校验（v1.5 S1 / D-3）。

隐私红线要求"不下发 visit 结束后仍可访问的 URL"，所以这里**不发长期 URL**：每次读接口
逐条重签一个短 TTL、窄 scope 的签名 URL。

```text
GET /v1/media/{media_id}?exp=<unix>&scope=<scope>&sig=<hex>
sig = HMAC-SHA256(media_url_signing_secret, f"{media_id}|{scope}|{exp}")
```

两个刻意的设计取舍：

1. **签名是唯一凭据，不要求 ``Authorization`` 头**。图片/音频组件带 header 是跨端常见坑，
   代价换成短 TTL + 窄 scope。因此 scope 必须窄到"这个人/这次拜访"，不能是"这个媒体"。
2. **secret 没有弱默认值**。留空时签发与校验都直接抛错，并在启动时（媒体开关为开）
   直接 fail fast——"忘配 secret 却签得出 URL"比启动失败危险得多。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger("ai4all.media.access")

SCOPE_OWNER_PREFIX = "pu:"
SCOPE_VISIT_PREFIX = "visit:"

# 时钟偏差容忍：签发方与校验方是同一台机，给 1 分钟只为吸收请求在途时间。
_EXP_SKEW_SECONDS = 60


class MediaAccessDeniedError(RuntimeError):
    """签名无效、过期或 scope 已失效；对外恒为 ``media_access_denied``，不区分原因。"""

    code = "media_access_denied"


class MediaSigningNotConfiguredError(RuntimeError):
    """``MEDIA_URL_SIGNING_SECRET`` 未配置；属于部署错误，不对客户端暴露细节。"""

    code = "media_signing_not_configured"


@dataclass(frozen=True)
class MediaAccessGrant:
    """一次签名下发的结果；``url`` 是客户端直接可用的相对路径。"""

    media_id: str
    scope: str
    expires_at: int
    signature: str
    url: str


def owner_scope(platform_user_id: str) -> str:
    """主人自己看：``pu:<platform_user_id>``。"""
    cleaned = str(platform_user_id or "").strip()
    if not cleaned:
        raise ValueError("platform_user_id is required")
    return f"{SCOPE_OWNER_PREFIX}{cleaned}"


def visit_scope(visit_id: str) -> str:
    """访客看：``visit:<visit_id>``；visit 一终止，该 scope 立刻失效。"""
    cleaned = str(visit_id or "").strip()
    if not cleaned:
        raise ValueError("visit_id is required")
    return f"{SCOPE_VISIT_PREFIX}{cleaned}"


def signing_secret() -> str:
    """取签名密钥；未配置即抛错（**不给弱默认值**）。"""
    secret = str(getattr(settings, "media_url_signing_secret", "") or "").strip()
    if not secret:
        raise MediaSigningNotConfiguredError("MEDIA_URL_SIGNING_SECRET is not configured")
    return secret


def signing_configured() -> bool:
    """secret 是否已配置。媒体能力的「配置就绪」判据，不抛错，供能力位与门控调用。"""
    return bool(str(getattr(settings, "media_url_signing_secret", "") or "").strip())


def media_features_enabled() -> bool:
    """三个媒体能力位里有没有开着的（决定 secret 是否为必填）。"""
    return any(
        bool(getattr(settings, flag, False))
        for flag in (
            "companion_world_chat_image_enabled",
            "companion_world_chat_voice_enabled",
            "companion_world_feed_image_enabled",
        )
    )


def validate_media_signing_config() -> None:
    """启动期校验：开关为开而 secret 留空 → 只告警，媒体能力按「未就绪」对外呈现。

    三个媒体开关自 2026-07-30 起默认为 True（开关默认极性约定），此时「secret 未配」是
    尚未配置的常态而非部署事故，起不来反而会拖垮整个服务；真正的 fail-closed 落在下游：
    能力位下发 false、上传返回 ``media_disabled``、签名调用抛
    ``MediaSigningNotConfiguredError``，不存在"没有密钥却签得出 URL"的路径。
    生成方式见 `.env.example`：``python -c "import secrets;print(secrets.token_urlsafe(32))"``。
    """
    if not media_features_enabled() or signing_configured():
        return
    logger.warning(
        "media_signing_secret_missing: 媒体能力按未就绪下发（能力位 false / 上传 media_disabled），"
        "配置 MEDIA_URL_SIGNING_SECRET 后重启即生效"
    )


def _sign(*, media_id: str, scope: str, expires_at: int) -> str:
    payload = f"{media_id}|{scope}|{int(expires_at)}".encode("utf-8")
    return hmac.new(signing_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_media_url(
    *,
    media_id: str,
    scope: str,
    ttl_seconds: int,
    now: Optional[int] = None,
    path_prefix: str = "/v1/media",
) -> MediaAccessGrant:
    """签发一个短 TTL 读 URL。``ttl_seconds`` 由调用方按 owner/visitor 口径算好传进来。"""
    cleaned_id = str(media_id or "").strip()
    cleaned_scope = str(scope or "").strip()
    if not cleaned_id or not cleaned_scope:
        raise ValueError("media_id and scope are required")
    ttl = int(ttl_seconds)
    if ttl <= 0:
        # 访客 TTL 取 min(配置, visit 剩余)，visit 已过期时会算出 <=0：这不是配置错误，
        # 而是"这个人此刻无权看"，交给调用方映射 media_access_denied。
        raise MediaAccessDeniedError("media url ttl is not positive")
    expires_at = int(now if now is not None else time.time()) + ttl
    signature = _sign(media_id=cleaned_id, scope=cleaned_scope, expires_at=expires_at)
    url = (
        f"{path_prefix}/{cleaned_id}"
        f"?exp={expires_at}&scope={cleaned_scope}&sig={signature}"
    )
    return MediaAccessGrant(
        media_id=cleaned_id,
        scope=cleaned_scope,
        expires_at=expires_at,
        signature=signature,
        url=url,
    )


def verify_media_signature(
    *,
    media_id: str,
    scope: str,
    expires_at: int,
    signature: str,
    now: Optional[int] = None,
) -> None:
    """校验签名与有效期；失败抛 :class:`MediaAccessDeniedError`。

    scope 当前是否**仍然**有权访问该媒体（访客要复查 visit 未终止）由调用方判定——
    本函数只回答"这个 URL 是我们签的且没过期"。
    """
    cleaned_id = str(media_id or "").strip()
    cleaned_scope = str(scope or "").strip()
    cleaned_sig = str(signature or "").strip()
    if not cleaned_id or not cleaned_scope or not cleaned_sig:
        raise MediaAccessDeniedError("media signature is incomplete")
    try:
        exp = int(expires_at)
    except (TypeError, ValueError) as err:
        raise MediaAccessDeniedError("media signature exp is malformed") from err
    expected = _sign(media_id=cleaned_id, scope=cleaned_scope, expires_at=exp)
    # 定长比较，避免时序侧信道泄漏签名前缀。
    if not hmac.compare_digest(expected, cleaned_sig):
        raise MediaAccessDeniedError("media signature mismatch")
    current = int(now if now is not None else time.time())
    if exp + _EXP_SKEW_SECONDS < current:
        raise MediaAccessDeniedError("media signature expired")


def owner_ttl_seconds() -> int:
    """主人 TTL（默认 15 分钟）。"""
    return max(int(getattr(settings, "media_url_owner_ttl_seconds", 900) or 900), 1)


def visitor_ttl_seconds(*, visit_remaining_seconds: Optional[int]) -> int:
    """访客 TTL = ``min(配置值, visit 剩余时长)``；visit 已结束会算出 <= 0。"""
    configured = max(int(getattr(settings, "media_url_visitor_ttl_seconds", 600) or 600), 1)
    if visit_remaining_seconds is None:
        return configured
    return min(configured, int(visit_remaining_seconds))


__all__ = [
    "MediaAccessDeniedError",
    "MediaAccessGrant",
    "MediaSigningNotConfiguredError",
    "SCOPE_OWNER_PREFIX",
    "SCOPE_VISIT_PREFIX",
    "media_features_enabled",
    "owner_scope",
    "owner_ttl_seconds",
    "sign_media_url",
    "signing_configured",
    "signing_secret",
    "validate_media_signing_config",
    "verify_media_signature",
    "visit_scope",
    "visitor_ttl_seconds",
]
