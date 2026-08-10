"""Plum Create 角色立绘上传与创作者私有预览。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from app.config import settings
from app.db import SessionPrincipal
from app.platform.media.assets import (
    MEDIA_KIND_IMAGE,
    MediaDecodeFailedError,
    MediaKindUnsupportedError,
    MediaTooLargeError,
    build_storage_path,
    delete_media_file,
    new_media_id,
    normalize_creator_portrait,
    read_media_file,
    sha256_hex,
    write_media_file,
)
from app.platform.media.persistence import (
    get_media_asset,
    insert_media_asset,
    pending_expires_at,
)
from app.platform.quota.rate_limiter import rate_limiter
from app.products.plum.api.deps import require_plum_principal

router = APIRouter(tags=["plum-creator-media"])

_UPLOAD_RPM_LIMIT = 30
_PREVIEW_PATH_PREFIX = "/api/v1/products/plum/creator/media"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


@router.post("/creator/media/uploads")
async def upload_creator_portrait(
    response: Response,
    file: UploadFile = File(...),
    kind: str = Form(default="image"),
    purpose: str = Form(default="character_portrait"),
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    """解码并标准化 Create V1 立绘，返回 owner-only 预览地址。"""

    if str(kind or "").strip().lower() != MEDIA_KIND_IMAGE:
        raise HTTPException(status_code=415, detail="media_kind_unsupported")
    if str(purpose or "").strip().lower() != "character_portrait":
        raise HTTPException(status_code=422, detail="creator_media_purpose_invalid")
    if not rate_limiter.check_rpm(
        f"plum_creator_media:{principal.platform_user_id}", _UPLOAD_RPM_LIMIT
    ):
        raise HTTPException(status_code=429, detail="rate_limited")

    max_bytes = int(settings.media_image_max_bytes)
    raw = await file.read(max_bytes + 1)
    await file.close()
    if not raw:
        raise HTTPException(status_code=422, detail="media_content_required")
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="media_too_large")

    try:
        normalized = normalize_creator_portrait(raw)
    except MediaTooLargeError as err:
        raise HTTPException(status_code=413, detail=err.code) from err
    except MediaKindUnsupportedError as err:
        raise HTTPException(status_code=415, detail=err.code) from err
    except MediaDecodeFailedError as err:
        raise HTTPException(status_code=422, detail=err.code) from err

    media_id = new_media_id()
    digest = sha256_hex(normalized.data)
    storage_path = build_storage_path(media_id=media_id, sha256=digest)
    write_media_file(storage_path=storage_path, data=normalized.data)
    try:
        asset = insert_media_asset(
            media_id=media_id,
            owner_platform_user_id=principal.platform_user_id,
            kind=MEDIA_KIND_IMAGE,
            mime=normalized.mime,
            bytes_len=len(normalized.data),
            sha256=digest,
            storage_path=storage_path,
            width=normalized.width,
            height=normalized.height,
            expires_at=pending_expires_at(
                ttl_hours=int(settings.media_pending_ttl_hours)
            ),
        )
    except BaseException:
        delete_media_file(storage_path)
        raise

    _no_store(response)
    return {
        "status": "ok",
        "media": {
            "media_id": str(asset["id"]),
            "mime": str(asset["mime"]),
            "bytes": int(asset["bytes"]),
            "width": int(asset["width"]),
            "height": int(asset["height"]),
            "preview_url": f"{_PREVIEW_PATH_PREFIX}/{asset['id']}",
            "pending_expires_at": asset["expires_at"],
        },
    }


@router.get("/creator/media/{media_id}")
def read_creator_media(
    media_id: str,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> Response:
    """只允许 owner 读取尚未发布的 Create 媒体，跨账号统一返回 404。"""

    asset = get_media_asset(
        media_id=media_id,
        owner_platform_user_id=principal.platform_user_id,
    )
    if asset is None or str(asset.get("kind")) != MEDIA_KIND_IMAGE:
        raise HTTPException(status_code=404, detail="creator_media_not_found")
    try:
        payload = read_media_file(str(asset["storage_path"]))
    except (FileNotFoundError, ValueError) as err:
        raise HTTPException(status_code=404, detail="creator_media_not_found") from err
    return Response(
        content=payload,
        media_type=str(asset["mime"]),
        headers={"Cache-Control": "private, no-store"},
    )


__all__ = ["router"]
