"""媒体上传与签名读端点（v1.5 S1 / D-3、D-4、D-5）。

两个端点、两套鉴权，刻意不同：

- ``POST /media/uploads`` 走标准会话（``Authorization: Bearer``），落盘 + 建 ``pending`` 资产行。
- ``GET /media/{media_id}`` **只认 URL 签名**，不要求 ``Authorization``——图片/音频组件带 header
  是跨端常见坑。代价换成短 TTL（主人 15 分钟 / 访客 ≤10 分钟且不超过 visit 剩余）+ 窄 scope。

上传成功只意味着"字节已存下"，不意味着"已发出去"：资产处于 ``pending``，2 小时内未被任何
消息/动态引用就连行带文件回收（D-10）。发送时的引用与状态翻转在 S2/S3 落地。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import SessionPrincipal
from app.platform.media.access import (
    MediaAccessDeniedError,
    MediaSigningNotConfiguredError,
    SCOPE_OWNER_PREFIX,
    SCOPE_VISIT_PREFIX,
    owner_scope,
    owner_ttl_seconds,
    sign_media_url,
    signing_configured,
    verify_media_signature,
)
from app.platform.media.asr import ASRError, transcribe_audio
from app.platform.media.assets import (
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VOICE,
    MediaError,
    delete_media_file,
    new_media_id,
    normalize_voice,
    read_media_file,
    build_storage_path,
    sha256_hex,
    strip_image_metadata,
    voice_extension,
    write_media_file,
)
from app.platform.media.persistence import (
    get_media_asset_unscoped,
    insert_media_asset,
    pending_expires_at,
)
from app.platform.quota.rate_limiter import rate_limiter
from app.products.zhaoxi.api.companion_world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
)
from app.products.zhaoxi.api.contracts import WORLD_ERROR_RESPONSES, MediaUploadResponse
from app.products.zhaoxi.infrastructure.persistence.companion_world_visits import (
    get_universe_visit,
)
from app.time_utils import beijing_naive_now

logger = logging.getLogger("ai4all.products.zhaoxi.media")

router = APIRouter(tags=["companion-world-media"])

_BEIJING_TZ = timezone(timedelta(hours=8))

# 上传频次上限（每真人每分钟）。刻意做成模块常量而非配置项：它是防磁盘打满的安全网，
# 不是产品可调旋钮。4 图动态 + 重试也远够用。
_UPLOAD_RPM_LIMIT = 30

# 上传端点特有的 HTTP 码（世界端点通用错误见 WORLD_ERROR_RESPONSES）。
_UPLOAD_ERROR_RESPONSES = {
    413: {"description": "超出字节或时长上限（media_too_large / media_duration_exceeded）"},
    415: {"description": "格式不在白名单（media_kind_unsupported）"},
}

# 运行时必须自行读取 query，才能把「缺参数/类型错误」也统一映射成 media_access_denied；
# OpenAPI 仍需如实声明三项必填，避免客户端生成器误以为它们可省略。
_MEDIA_ACCESS_QUERY_PARAMETERS = [
    {
        "name": "exp",
        "in": "query",
        "required": True,
        "description": "签名过期时间（unix 秒）",
        "schema": {
            "type": "integer",
            "title": "Exp",
            "description": "签名过期时间（unix 秒）",
        },
    },
    {
        "name": "scope",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "title": "Scope",
            "minLength": 1,
            "maxLength": 200,
        },
    },
    {
        "name": "sig",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "title": "Sig",
            "minLength": 16,
            "maxLength": 128,
        },
    },
]


def _public_time(value: Optional[str]) -> Optional[str]:
    """DB 北京 naive 串 → 带 +08:00 的公开 ISO 时间（TIME-001）。"""
    if not value:
        return None
    return (
        datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=_BEIJING_TZ)
        .isoformat()
    )


def _epoch_to_public_time(value: int) -> str:
    """签名过期（unix 秒）→ 带 +08:00 的公开 ISO 时间，与其余时间字段同口径。"""
    return datetime.fromtimestamp(int(value), tz=_BEIJING_TZ).isoformat()


def _image_upload_enabled() -> bool:
    """图片上传只要"聊天图片"或"图文动态"任一开着就允许——上传是共用地基。"""
    return signing_configured() and bool(
        getattr(settings, "companion_world_chat_image_enabled", False)
        or getattr(settings, "companion_world_feed_image_enabled", False)
    )


def _voice_upload_enabled() -> bool:
    # 与 /app/config 的能力位同一判据：未配签名密钥时整条媒体链路视为未就绪。
    return signing_configured() and bool(
        getattr(settings, "companion_world_chat_voice_enabled", False)
    )


def _require_media_enabled(kind: str) -> None:
    """按 kind 分别门控；关闭时返回 ``media_disabled`` 而不是 404，客户端好分支。"""
    if kind == MEDIA_KIND_IMAGE and _image_upload_enabled():
        return
    if kind == MEDIA_KIND_VOICE and _voice_upload_enabled():
        return
    raise CompanionWorldApiError("media_disabled")


def _sign_owner_url(*, media_id: str, platform_user_id: str):
    """给主人签一条读 URL；secret 未配置属于部署错误，映射 503。"""
    try:
        return sign_media_url(
            media_id=media_id,
            scope=owner_scope(platform_user_id),
            ttl_seconds=owner_ttl_seconds(),
        )
    except MediaSigningNotConfiguredError as err:
        logger.error("media_signing_secret_missing")
        raise CompanionWorldApiError("media_signing_unavailable") from err


async def _transcribe_voice_best_effort(
    *, content: bytes, mime: str, filename: Optional[str]
) -> Optional[str]:
    """在线程池同步等待转写；失败一律吞掉（D-5：不阻塞发送结果）。"""
    safe_name = filename or f"recording{voice_extension(mime)}"
    try:
        return (
            await run_in_threadpool(
                transcribe_audio,
                content=content,
                filename=safe_name,
                content_type=mime,
            )
            or None
        )
    except ASRError as err:
        logger.warning("media_voice_transcribe_failed error_type=%s", type(err).__name__)
        return None
    except Exception as err:  # provider 之外的意外错误同样不能阻塞上传
        logger.warning("media_voice_transcribe_crashed error_type=%s", type(err).__name__)
        return None


@router.post(
    "/media/uploads",
    response_model=MediaUploadResponse,
    responses={**WORLD_ERROR_RESPONSES, **_UPLOAD_ERROR_RESPONSES},
)
async def upload_media(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    kind: str = Form(..., description="image | voice"),
    duration_ms: Optional[int] = Form(default=None, ge=1),
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    """上传一份媒体资产，返回 ``media_ref`` 与一条短 TTL 的主人读 URL。

    服务端不信任客户端声明：图片一律重编码并剥离 EXIF/GPS（D-4），语音容器按魔数复核。
    ``duration_ms`` 由客户端声明（与 ``/audio/transcriptions`` 同口径），仅做上限校验。
    """
    cleaned_kind = str(kind or "").strip().lower()
    if cleaned_kind not in {MEDIA_KIND_IMAGE, MEDIA_KIND_VOICE}:
        raise CompanionWorldApiError("media_kind_unsupported")
    _require_media_enabled(cleaned_kind)
    if not rate_limiter.check_rpm(f"media_upload:{principal.platform_user_id}", _UPLOAD_RPM_LIMIT):
        raise CompanionWorldApiError("rate_limited")

    if cleaned_kind == MEDIA_KIND_IMAGE:
        max_bytes = int(settings.media_image_max_bytes)
    else:
        max_bytes = int(settings.media_voice_max_bytes)
        if duration_ms is not None and int(duration_ms) > int(
            settings.media_voice_max_duration_ms
        ):
            raise CompanionWorldApiError("media_duration_exceeded")
    # 多读 1 字节用于判超限：超限的请求不需要把整份数据读完。
    raw = await file.read(max_bytes + 1)
    await file.close()
    if not raw:
        raise CompanionWorldApiError("media_content_required")
    if len(raw) > max_bytes:
        raise CompanionWorldApiError("media_too_large")

    declared_type = str(file.content_type or "application/octet-stream").lower()
    try:
        if cleaned_kind == MEDIA_KIND_IMAGE:
            normalized = strip_image_metadata(raw)
            payload = normalized.data
            mime = normalized.mime
            width: Optional[int] = normalized.width
            height: Optional[int] = normalized.height
            resolved_duration: Optional[int] = None
        else:
            voice = normalize_voice(
                raw=raw, content_type=declared_type, duration_ms=duration_ms
            )
            payload = voice.data
            mime = voice.mime
            width = height = None
            resolved_duration = voice.duration_ms
    except MediaError as err:
        raise CompanionWorldApiError(err.code) from err

    media_id = new_media_id()
    digest = sha256_hex(payload)
    storage_path = build_storage_path(media_id=media_id, sha256=digest)
    # 先落盘再写库：反过来会出现"库里有行、磁盘无文件"的坏读。落盘成功但写库失败留下的
    # 孤儿文件在这里就地清掉，不留给回收 job（它按库行扫，扫不到无主文件）。
    write_media_file(storage_path=storage_path, data=payload)
    transcript: Optional[str] = None
    if cleaned_kind == MEDIA_KIND_VOICE:
        transcript = await _transcribe_voice_best_effort(
            content=payload, mime=mime, filename=file.filename
        )
    try:
        asset = insert_media_asset(
            media_id=media_id,
            owner_platform_user_id=principal.platform_user_id,
            kind=cleaned_kind,
            mime=mime,
            bytes_len=len(payload),
            sha256=digest,
            storage_path=storage_path,
            width=width,
            height=height,
            duration_ms=resolved_duration,
            transcript=transcript,
            expires_at=pending_expires_at(ttl_hours=int(settings.media_pending_ttl_hours)),
        )
    except BaseException:
        delete_media_file(storage_path)
        raise

    grant = _sign_owner_url(media_id=media_id, platform_user_id=principal.platform_user_id)
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "media_id": asset["id"],
            "kind": asset["kind"],
            "mime": asset["mime"],
            "bytes": int(asset["bytes"]),
            "width": asset["width"],
            "height": asset["height"],
            "duration_ms": asset["duration_ms"],
            "transcript": asset["transcript"],
            "expires_at": _public_time(asset["expires_at"]),
            "url": grant.url,
            "url_expires_at": _epoch_to_public_time(grant.expires_at),
        },
    )


def _visit_remaining_seconds(visit: dict) -> Optional[int]:
    """visit 剩余秒数；``expires_at`` 为空视为不限（pending 阶段不会走到读媒体）。"""
    raw = visit.get("expires_at")
    if not raw:
        return None
    try:
        deadline = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int((deadline - beijing_naive_now()).total_seconds())


def _authorize_scope(*, asset: dict, scope: str) -> bool:
    """签名之外的第二道闸：scope 此刻是否**仍然**有权访问这份媒体。

    - ``pu:<id>``：只能读自己拥有的媒体。
    - ``visit:<id>``：visit 必须仍处于 ``active`` 且未到期，且媒体属于该 visit 的任一参与方
      （主人看访客发来的图、访客看主人的动态图，都走这条）。visit 一终止立即失效。
    """
    owner_id = str(asset.get("owner_platform_user_id") or "")
    if scope.startswith(SCOPE_OWNER_PREFIX):
        return scope[len(SCOPE_OWNER_PREFIX):] == owner_id
    if not scope.startswith(SCOPE_VISIT_PREFIX):
        return False
    visit_id = scope[len(SCOPE_VISIT_PREFIX):]
    if not visit_id:
        return False
    visit = get_universe_visit(visit_id=visit_id)
    if visit is None or str(visit.get("status")) != "active":
        return False
    remaining = _visit_remaining_seconds(visit)
    if remaining is not None and remaining <= 0:
        return False
    return owner_id in {
        str(visit.get("owner_platform_user_id") or ""),
        str(visit.get("visitor_platform_user_id") or ""),
    }


@router.get(
    "/media/{media_id}",
    response_class=Response,
    responses={
        200: {"description": "媒体字节流", "content": {"application/octet-stream": {}}},
        **WORLD_ERROR_RESPONSES,
    },
    openapi_extra={"parameters": _MEDIA_ACCESS_QUERY_PARAMETERS},
)
def read_media(
    media_id: str,
    request: Request,
) -> Response:
    """按签名下发媒体字节流。**不读 ``Authorization``**，签名 URL 是唯一凭据（D-3）。

    任何失败（签名不符、过期、scope 失效、行或文件缺失）统一收敛到 ``media_access_denied``，
    不给资源枚举信号。
    """
    try:
        # 访问凭据的任何形状错误都必须与验签失败同码，不能让 FastAPI 参数校验提前泄漏成
        # 422 invalid_request。这里收口缺失、非整数 exp、空 scope/sig 与长度异常。
        exp = request.query_params.get("exp")
        cleaned_scope = str(request.query_params.get("scope") or "").strip()
        cleaned_sig = str(request.query_params.get("sig") or "").strip()
        if (
            not cleaned_scope
            or len(cleaned_scope) > 200
            or not (16 <= len(cleaned_sig) <= 128)
        ):
            raise MediaAccessDeniedError("invalid media access parameters")
        expires_at = int(str(exp or "").strip())
        verify_media_signature(
            media_id=media_id,
            scope=cleaned_scope,
            expires_at=expires_at,
            signature=cleaned_sig,
        )
    except (MediaAccessDeniedError, TypeError, ValueError) as err:
        raise CompanionWorldApiError("media_access_denied") from err
    except MediaSigningNotConfiguredError as err:
        logger.error("media_signing_secret_missing")
        raise CompanionWorldApiError("media_signing_unavailable") from err

    asset = get_media_asset_unscoped(media_id=media_id)
    if asset is None or not _authorize_scope(asset=asset, scope=cleaned_scope):
        raise CompanionWorldApiError("media_access_denied")
    try:
        payload = read_media_file(str(asset["storage_path"]))
    except (FileNotFoundError, ValueError):
        # 库里有行、磁盘没文件：只可能是人工干预或回收竞态。对外仍是拒绝，不暴露内部状态。
        logger.warning("media_file_missing media_id=%s", media_id)
        raise CompanionWorldApiError("media_access_denied")

    is_owner_scope = cleaned_scope.startswith(SCOPE_OWNER_PREFIX)
    headers = {
        # 主人可短时缓存；访客一律 no-store——visit 结束后连本地缓存都不该留（§3.3 隐私红线）。
        "Cache-Control": "private, max-age=600" if is_owner_scope else "no-store, private",
        "Content-Length": str(len(payload)),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=payload, media_type=str(asset["mime"]), headers=headers)


__all__ = ["router"]
