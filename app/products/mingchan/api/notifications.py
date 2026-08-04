"""鸣蝉 App 拉取式通知 API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.bootstrap.product_registry import PRODUCTION_PRODUCT_REGISTRY, ProductRegistry
from app.config import settings
from app.db import SessionPrincipal
from app.products.mingchan.api.deps import (
    build_mingchan_enabled_dependency,
    build_mingchan_session_dependency,
)
from app.products.mingchan.api.world_onboarding import (
    MingchanWorldApiError,
    _envelope,
    _no_store,
    _run_domain,
)
from app.products.mingchan.api.world_contracts import (
    NotificationPreferencesResponse,
    WORLD_ERROR_RESPONSES,
)
from app.products.mingchan.domain.notifications import MingchanNotificationService
from app.products.mingchan.infrastructure import notification_preferences
from app.products.mingchan.infrastructure.notifications import (
    SqlMingchanNotificationRepository,
)
from app.time_utils import beijing_now

_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


class EmptyPayload(BaseModel):
    """显式拒绝已读端点中的 owner/account 注入字段。"""

    model_config = ConfigDict(extra="forbid")


class NotificationPreferencesPayload(BaseModel):
    """鸣蝉通知安静程度更新请求。"""

    model_config = ConfigDict(extra="forbid")

    quiet_level: str = Field(max_length=32)


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
    """序列化公开通知 DTO，不暴露 claim、fingerprint 或 runtime 字段。"""

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
        "body": {"type": "text", "text": item.body_text or ""},
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
        raise MingchanWorldApiError("invalid_cursor")
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
        raise MingchanWorldApiError("invalid_cursor") from None
    return delivered_at, notification_id


def build_router(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    *,
    config=None,
) -> APIRouter:
    """创建固定鸣蝉 audience、固定产品查询作用域的通知 router。"""

    notification_config = settings if config is None else config
    require_enabled = build_mingchan_enabled_dependency(registry)
    require_session = build_mingchan_session_dependency(registry)

    def require_notification_session(
        authorization: Optional[str] = Header(default=None),
    ) -> SessionPrincipal:
        if not bool(
            getattr(notification_config, "mingchan_p1_enabled", False)
        ) or not bool(
            getattr(
                notification_config,
                "mingchan_app_inbox_enabled",
                False,
            )
        ):
            raise MingchanWorldApiError("feature_disabled")
        try:
            return require_session(authorization=authorization)
        except HTTPException as err:
            raise MingchanWorldApiError("unauthorized", 401) from err

    def service() -> MingchanNotificationService:
        return MingchanNotificationService(SqlMingchanNotificationRepository())

    router = APIRouter(
        tags=["mingchan-app-notifications"],
        dependencies=[Depends(require_enabled)],
    )

    @router.get("/notifications")
    def list_notifications(
        request: Request,
        response: Response,
        status: str = Query(default="all", pattern="^(all|unread)$"),
        cursor: Optional[str] = Query(default=None, max_length=1024),
        limit: int = Query(default=20, ge=1, le=50),
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        cursor_delivered_at, cursor_notification_id = _decode_cursor(cursor)
        now = _db_time(beijing_now())
        rows = _run_domain(
            lambda: service().list_notifications(
                principal.platform_user_id,
                now=now,
                status=status,
                cursor_delivered_at=cursor_delivered_at,
                cursor_notification_id=cursor_notification_id,
                limit=limit + 1,
            )
        )
        page = rows[:limit]
        unread = service().count_unread(principal.platform_user_id, now=now)
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={
                "items": [_item_data(item) for item in page],
                "unread_count": unread,
                "next_cursor": (
                    _encode_cursor(page[-1]) if len(rows) > limit and page else None
                ),
            },
        )

    @router.get("/notifications/unread-count")
    def unread_count(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        count = service().count_unread(
            principal.platform_user_id,
            now=_db_time(beijing_now()),
        )
        _no_store(response)
        return _envelope(request, code="ok", data={"unread_count": count})

    @router.post("/notifications/{notification_id}/read")
    def mark_read(
        notification_id: str,
        request: Request,
        response: Response,
        payload: Optional[EmptyPayload] = None,
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        current = beijing_now().astimezone(_BEIJING_TZ).replace(
            tzinfo=None,
            microsecond=0,
        )
        now = current.strftime("%Y-%m-%d %H:%M:%S")
        item = _run_domain(
            lambda: service().mark_read(
                principal.platform_user_id,
                notification_id=notification_id,
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
            data={"notification": _item_data(item)},
        )

    @router.post("/notifications/read-all")
    def mark_all_read(
        request: Request,
        response: Response,
        payload: Optional[EmptyPayload] = None,
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        current = beijing_now().astimezone(_BEIJING_TZ).replace(
            tzinfo=None,
            microsecond=0,
        )
        now = current.strftime("%Y-%m-%d %H:%M:%S")
        marked_count, read_at = _run_domain(
            lambda: service().mark_all_read(
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

    @router.get(
        "/notifications/preferences",
        response_model=NotificationPreferencesResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def get_preferences(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        """读取鸣蝉产品通知偏好；缺行返回默认设置。"""

        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={
                **notification_preferences.get_notification_preferences(
                    platform_user_id=principal.platform_user_id
                ),
                "available_levels": list(notification_preferences.QUIET_LEVELS),
            },
        )

    @router.patch(
        "/notifications/preferences",
        response_model=NotificationPreferencesResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def update_preferences(
        payload: NotificationPreferencesPayload,
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_notification_session),
    ) -> dict:
        """幂等更新鸣蝉产品通知偏好。"""

        current = beijing_now().astimezone(_BEIJING_TZ).replace(
            tzinfo=None,
            microsecond=0,
        )
        try:
            result = notification_preferences.set_notification_preferences(
                platform_user_id=principal.platform_user_id,
                quiet_level=payload.quiet_level,
                now=current,
            )
        except ValueError as err:
            raise MingchanWorldApiError("quiet_level_invalid", 422) from err
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={**result, "available_levels": list(notification_preferences.QUIET_LEVELS)},
        )

    return router


__all__ = ["build_router"]
