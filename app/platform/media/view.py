"""消息媒体的**展示层投影**（v1.5 S2 / D-1、D-2）。

一条消息在库里被拆成三份，这里负责把它们拼回客户端要的那一个判别联合：

| 来源 | 内容 |
| --- | --- |
| ``messages.content`` / ``human_messages.body_text`` | LLM 上下文文本（图片轮含 VL 描述） |
| ``messages.content_json`` | 展示载荷：``{"type", "text"}``，``text`` 是**用户自己写的 caption** |
| ``media_assets`` | 尺寸 / 时长 / 转写 / mime |

**为什么 ``content_json`` 里只存 type 与 caption**：URL 是短 TTL 签名的，存进库第二天就过期；
宽高时长存两份必然漂移。所以库里只留"不可再生"的那一份（caption——它和 LLM 上下文文本不是
同一个字符串，见 D-2 红线），其余每次读时从 ``media_assets`` 现取、URL 现签。

**签名 scope 由调用方给**：自己的媒体用 ``pu:<自己>``，对方的媒体用 ``visit:<visit_id>``
（visit 一结束立即失效）。本模块不猜 scope——猜错就是隐私事故。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional

from app.platform.media.access import (
    MediaAccessDeniedError,
    MediaSigningNotConfiguredError,
    sign_media_url,
)
from app.platform.media.assets import MEDIA_KIND_IMAGE, MEDIA_KIND_VOICE

logger = logging.getLogger("ai4all.media.view")

CONTENT_TYPE_TEXT = "text"
CONTENT_TYPE_IMAGE = "image"
CONTENT_TYPE_AUDIO = "audio"

# 资产 kind → 对外 content.type。语音对外叫 ``audio``（与客户端既有音频组件命名一致），
# 库内 kind 叫 ``voice``（与"语音消息"的产品叫法一致），两个词表刻意不合并。
_CONTENT_TYPE_BY_KIND = {
    MEDIA_KIND_IMAGE: CONTENT_TYPE_IMAGE,
    MEDIA_KIND_VOICE: CONTENT_TYPE_AUDIO,
}

# 无 caption 的媒体消息在会话列表里的预览占位。
_PREVIEW_BY_KIND = {MEDIA_KIND_IMAGE: "[图片]", MEDIA_KIND_VOICE: "[语音]"}
_PREVIEW_BY_CONTENT_TYPE = {CONTENT_TYPE_IMAGE: "[图片]", CONTENT_TYPE_AUDIO: "[语音]"}

__all__ = [
    "CONTENT_TYPE_AUDIO",
    "CONTENT_TYPE_IMAGE",
    "CONTENT_TYPE_TEXT",
    "build_media_content",
    "build_stored_content",
    "content_type_for_kind",
    "decode_stored_content",
    "media_preview_text",
    "stored_content_preview",
    "text_content",
]


def content_type_for_kind(kind: Optional[str]) -> Optional[str]:
    """``image``/``voice`` → 对外 ``content.type``；未知 kind 返回 None。"""
    return _CONTENT_TYPE_BY_KIND.get(str(kind or "").strip())


def text_content(text: Optional[str]) -> Dict[str, Any]:
    """纯文本消息的 content。存量消息（``content_json`` 为空）也走这里。"""
    return {"type": CONTENT_TYPE_TEXT, "text": str(text or "")}


def build_stored_content(*, kind: str, caption: Optional[str]) -> Dict[str, Any]:
    """落库用的展示载荷；**只含 type 与 caption**，不含 URL 与任何服务端生成文本。"""
    content_type = content_type_for_kind(kind)
    if content_type is None:
        raise ValueError(f"unsupported media kind: {kind}")
    return {"type": content_type, "text": str(caption or "")}


def decode_stored_content(raw: Any) -> Optional[Dict[str, Any]]:
    """解析 ``messages.content_json``；脏数据一律当作"没有展示载荷"降级为文本。"""
    if raw is None or raw == "":
        return None
    value = raw
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            logger.warning("content_json_decode_failed")
            return None
    if not isinstance(value, Mapping):
        return None
    content_type = str(value.get("type") or "").strip()
    if content_type not in {CONTENT_TYPE_TEXT, CONTENT_TYPE_IMAGE, CONTENT_TYPE_AUDIO}:
        return None
    return {"type": content_type, "text": str(value.get("text") or "")}


def media_preview_text(*, caption: Optional[str], kind: Optional[str]) -> str:
    """会话列表预览。

    红线：**绝不能拿 ``messages.content`` 当预览**——图片轮那一列含 VL 描述，
    直接展示等于把服务端生成的文本冒充成用户自己发的话（D-2）。
    """
    cleaned = " ".join(str(caption or "").split())
    if cleaned:
        return cleaned
    return _PREVIEW_BY_KIND.get(str(kind or "").strip(), "")


def stored_content_preview(
    *, raw_content_json: Any, fallback_text: Optional[str]
) -> Optional[str]:
    """按 ``messages.content_json`` 算会话列表预览；非媒体消息回落到原文。

    ``fallback_text`` 只在 ``content_json`` 为空（v1.5 之前的存量文本消息）时使用——
    媒体消息**绝不能**回落到它，那一列含 VL 描述（D-2 红线，同 :func:`media_preview_text`）。
    """
    stored = decode_stored_content(raw_content_json)
    if stored is None or stored["type"] == CONTENT_TYPE_TEXT:
        return fallback_text
    cleaned = " ".join(stored["text"].split())
    return cleaned or _PREVIEW_BY_CONTENT_TYPE.get(stored["type"], "")


def build_media_content(
    *,
    asset: Mapping[str, Any],
    caption: Optional[str],
    scope: str,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """把一条 ``media_assets`` 行投影成 D-1 的 ``content``，并现签一条读 URL。

    签不出 URL 时（部署缺 secret，或 visit 已经没有剩余时长）``url`` 下发 ``null`` 而不是
    抛错——一条媒体加载不出来不该让整页历史 500。客户端按占位处理，别当成文本消息。
    """
    kind = str(asset.get("kind") or "")
    content_type = content_type_for_kind(kind)
    if content_type is None:
        return text_content(caption)
    content: Dict[str, Any] = {
        "type": content_type,
        "text": str(caption or ""),
        "media_id": str(asset.get("id") or ""),
        "url": None,
    }
    if content_type == CONTENT_TYPE_IMAGE:
        content["width"] = asset.get("width")
        content["height"] = asset.get("height")
    else:
        content["duration_ms"] = asset.get("duration_ms")
        # transcript 是用户自己说的话，不是服务端生成内容，因此允许下发（D-2 的唯一例外）。
        content["transcript"] = asset.get("transcript")
    try:
        content["url"] = sign_media_url(
            media_id=str(asset.get("id") or ""), scope=scope, ttl_seconds=ttl_seconds
        ).url
    except MediaSigningNotConfiguredError:
        logger.error("media_signing_secret_missing media_id=%s", asset.get("id"))
    except (MediaAccessDeniedError, ValueError):
        # TTL <= 0：visit 已到期。此刻本就无权看，下发 null 与"签了也 403"等价。
        logger.info("media_url_not_signed media_id=%s", asset.get("id"))
    return content
