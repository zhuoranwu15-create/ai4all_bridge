"""Companion World M3 App 拉取式通知 owner API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import SessionPrincipal
from app.products.zhaoxi.domain.companion_world import CompanionWorldNotificationService
from app.products.zhaoxi.application import SqlAppNotificationRepository
from app.products.zhaoxi.api.contracts import NotificationPreferencesResponse
from app.products.zhaoxi.infrastructure.persistence import me_settings
from app.products.zhaoxi.api.companion_world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
    _run_domain,
)
from app.time_utils import beijing_now

router = APIRouter(tags=["app-notifications"])

_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


class EmptyPayload(BaseModel):
    """显式拒绝 read 端点中的 owner/account 注入字段。"""

    model_config = ConfigDict(extra="forbid")


class NotificationPreferencesPayload(BaseModel):
    """ME-10 通知偏好入参；取值表由 GET 下发，客户端不硬编码。"""

    model_config = ConfigDict(extra="forbid")

    quiet_level: str = Field(max_length=32)


def _require_notification_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "companion_world_app_inbox_enabled", False)):
        raise CompanionWorldApiError("feature_disabled", 404)
    return _require_world_session(authorization)


def _service() -> CompanionWorldNotificationService:
    return CompanionWorldNotificationService(SqlAppNotificationRepository())


def _db_time(value: datetime) -> str:
    return value.astimezone(_BEIJING_TZ).replace(tzinfo=None).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_BEIJING_TZ).isoformat()


def _item_data(item) -> dict:
    """序列化公开通知 DTO，不暴露 fingerprint/claim/metadata/runtime 字段。"""
    return {
        "notification_id": item.id,
        "category": item.category,
        "scope": item.scope,
        "resident": (
            {
                "resident_id": item.resident_id,
                "name": item.resident_name,
                "avatar_ref": item.resident_avatar_ref,
            }
            if item.resident_id
            else None
        ),
        "title": item.title,
        "body": {"type": "text", "text": item.body_text},
        "target": {"type": item.target_type, "id": item.target_id},
        "status": "read" if item.read_at else "unread",
        "read_at": _public_time(item.read_at),
        "created_at": _public_time(item.delivered_at),
        "expires_at": _public_time(item.expires_at),
    }


def _encode_cursor(item) -> str:
    payload = json.dumps(
        {"v": 1, "delivered_at": item.delivered_at, "id": item.id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if cursor is None:
        return None, None
    clean = cursor.strip()
    if not clean:
        raise CompanionWorldApiError("invalid_cursor")
    try:
        raw = base64.b64decode(
            (clean + "=" * (-len(clean) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"v", "delivered_at", "id"}:
            raise ValueError("invalid cursor shape")
        if value["v"] != 1:
            raise ValueError("invalid cursor version")
        delivered_at = str(value["delivered_at"])
        notification_id = str(value["id"])
        datetime.strptime(delivered_at, "%Y-%m-%d %H:%M:%S")
        if not _CURSOR_ID_RE.fullmatch(notification_id):
            raise ValueError("invalid cursor id")
    except (UnicodeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise CompanionWorldApiError("invalid_cursor")
    return delivered_at, notification_id


@router.get("/notifications")
def list_notifications(
    request: Request,
    response: Response,
    status: str = Query(default="all", pattern="^(all|unread)$"),
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    cursor_delivered_at, cursor_notification_id = _decode_cursor(cursor)
    now = _db_time(beijing_now())
    rows = _run_domain(
        lambda: _service().list_notifications(
            principal.platform_user_id,
            now=now,
            status=status,
            cursor_delivered_at=cursor_delivered_at,
            cursor_notification_id=cursor_notification_id,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    unread_count = _service().count_unread(principal.platform_user_id, now=now)
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [_item_data(item) for item in page],
            "unread_count": unread_count,
            "next_cursor": (
                _encode_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.get("/notifications/unread-count")
def unread_count(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    count = _service().count_unread(
        principal.platform_user_id, now=_db_time(beijing_now())
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"unread_count": count})


@router.post("/notifications/{notification_id}/read")
def mark_read(
    notification_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    current = beijing_now().astimezone(_BEIJING_TZ).replace(tzinfo=None, microsecond=0)
    now = current.strftime("%Y-%m-%d %H:%M:%S")
    item = _run_domain(
        lambda: _service().mark_read(
            principal.platform_user_id,
            notification_id=notification_id,
            now=now,
            read_expires_at=(current + timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"notification": _item_data(item)})


@router.post("/notifications/read-all")
def mark_all_read(
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    current = beijing_now().astimezone(_BEIJING_TZ).replace(tzinfo=None, microsecond=0)
    now = current.strftime("%Y-%m-%d %H:%M:%S")
    marked_count, read_at = _run_domain(
        lambda: _service().mark_all_read(
            principal.platform_user_id,
            now=now,
            read_expires_at=(current + timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"marked_count": marked_count, "read_at": _public_time(read_at)},
    )


@router.get("/notifications/preferences", response_model=NotificationPreferencesResponse)
def get_preferences(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    """读取通知安静程度（ME-10）；从未设置过时返回默认 ``standard``，读不写库。"""
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            **me_settings.get_notification_preferences(
                platform_user_id=principal.platform_user_id
            ),
            "available_levels": list(me_settings.QUIET_LEVELS),
        },
    )


@router.patch(
    "/notifications/preferences", response_model=NotificationPreferencesResponse
)
def update_preferences(
    payload: NotificationPreferencesPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_notification_session),
) -> dict:
    """设置通知安静程度（ME-10），幂等。

    ``quiet`` 只压制**将来**的投递；已在箱内的通知不回收——用户已经看到的东西不该
    因为改了偏好而消失。
    """
    current = beijing_now().astimezone(_BEIJING_TZ).replace(tzinfo=None, microsecond=0)
    try:
        result = me_settings.set_notification_preferences(
            platform_user_id=principal.platform_user_id,
            quiet_level=payload.quiet_level,
            now=current,
        )
    except ValueError as err:
        raise CompanionWorldApiError("quiet_level_invalid", 422) from err
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={**result, "available_levels": list(me_settings.QUIET_LEVELS)},
    )


__all__ = ["router"]
