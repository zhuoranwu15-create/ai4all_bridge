"""Companion World M4 私密 mailbox owner API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from app.bootstrap.product_registry import PRODUCTION_PRODUCT_REGISTRY, ProductRegistry
from app.config import settings
from app.db import SessionPrincipal
from app.products.mingchan.application.mailbox import (
    CompanionWorldMailboxService,
    MailboxError,
)
from app.products.mingchan.api.world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
)
from app.products.mingchan.api.world_contracts import (
    WORLD_ERROR_RESPONSES,
    MailboxLetterAcceptResponse,
    MailboxLetterDetailResponse,
    MailboxLetterListResponse,
    MailboxUnreadCountResponse,
)
from app.time_utils import beijing_naive_now

router = APIRouter(tags=["companion-world-mailbox"])
_mailbox_registry = PRODUCTION_PRODUCT_REGISTRY

_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


class EmptyPayload(BaseModel):
    """拒绝 owner/account/universe 注入字段。"""

    model_config = ConfigDict(extra="forbid")


def _require_mailbox_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "mingchan_mailbox_enabled", False)):
        raise CompanionWorldApiError("feature_disabled", 404)
    return _require_world_session(authorization)


def _service() -> CompanionWorldMailboxService:
    return CompanionWorldMailboxService(registry=_mailbox_registry)


def configure_runtime(registry: ProductRegistry) -> None:
    """注入可信鸣蝉产品注册表，供 mailbox 创建 resident account。"""

    global _mailbox_registry
    _mailbox_registry = registry


def _db_now() -> str:
    return beijing_naive_now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_BEIJING_TZ).isoformat()


def _item_data(item) -> dict:
    """严格白名单序列化，不返回 owner/catalog/policy/eligibility/runtime 字段。"""
    return {
        "letter_id": item.id,
        "character": {
            "name": item.character_name,
            "avatar_ref": item.avatar_ref,
            "summary": item.summary,
            "tags": list(item.tags),
        },
        "body": {"type": "text", "text": item.body_text},
        "status": item.status,
        "delivered_at": _public_time(item.delivered_at),
        "read_at": _public_time(item.read_at),
        "deferred_at": _public_time(item.deferred_at),
        "handled_at": _public_time(item.handled_at),
        "expires_at": _public_time(item.expires_at),
        "source": item.source,
        "wish_id": item.wish_id,
    }


def _resident_data(item: dict) -> dict:
    """序列化 accept 的 resident 白名单，不暴露 runtime/owner/template 字段。"""
    return {
        "resident_id": item["resident_id"],
        "name": item["name"],
        "avatar_ref": item.get("avatar_ref"),
        "status": item["status"],
        "origin": item["origin"],
        "conversation_id": item["conversation_id"],
        "conversation_state": item["conversation_state"],
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
    try:
        clean = cursor.strip()
        if not clean:
            raise ValueError("empty cursor")
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
        letter_id = str(value["id"])
        datetime.strptime(delivered_at, "%Y-%m-%d %H:%M:%S")
        if not _CURSOR_ID_RE.fullmatch(letter_id):
            raise ValueError("invalid cursor id")
        return delivered_at, letter_id
    except (UnicodeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise CompanionWorldApiError("invalid_cursor")


def _mailbox_call(action):
    try:
        return action()
    except MailboxError as err:
        raise CompanionWorldApiError(err.code) from err


@router.get(
    "/mailbox/letters",
    response_model=MailboxLetterListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def list_mailbox_letters(
    request: Request,
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    cursor_delivered_at, cursor_letter_id = _decode_cursor(cursor)
    statuses = (
        tuple(item.strip() for item in status_filter.split(",") if item.strip())
        if status_filter is not None
        else None
    )
    rows = _mailbox_call(
        lambda: _service().list_letters(
            platform_user.platform_user_id,
            now=_db_now(),
            statuses=statuses,
            cursor_delivered_at=cursor_delivered_at,
            cursor_letter_id=cursor_letter_id,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [_item_data(item) for item in page],
            "next_cursor": (
                _encode_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.get(
    "/mailbox/unread-count",
    response_model=MailboxUnreadCountResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def mailbox_unread_count(
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    count = _service().count_unread(platform_user.platform_user_id, now=_db_now())
    _no_store(response)
    return _envelope(request, code="ok", data={"unread_count": count})


@router.get(
    "/mailbox/letters/{letter_id}",
    response_model=MailboxLetterDetailResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def mailbox_letter_detail(
    letter_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    item = _mailbox_call(
        lambda: _service().get_letter(
            platform_user.platform_user_id, letter_id=letter_id, now=_db_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"letter": _item_data(item)})


def _transition_response(
    *,
    target_status: str,
    letter_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal,
) -> dict:
    item = _mailbox_call(
        lambda: _service().transition(
            platform_user.platform_user_id,
            letter_id=letter_id,
            target_status=target_status,
            now=_db_now(),
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"letter": _item_data(item)})


@router.post(
    "/mailbox/letters/{letter_id}/read",
    response_model=MailboxLetterDetailResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def read_mailbox_letter(
    letter_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    return _transition_response(
        target_status="read",
        letter_id=letter_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post(
    "/mailbox/letters/{letter_id}/defer",
    response_model=MailboxLetterDetailResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def defer_mailbox_letter(
    letter_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    return _transition_response(
        target_status="deferred",
        letter_id=letter_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post(
    "/mailbox/letters/{letter_id}/decline",
    response_model=MailboxLetterDetailResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def decline_mailbox_letter(
    letter_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    return _transition_response(
        target_status="declined",
        letter_id=letter_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post(
    "/mailbox/letters/{letter_id}/accept",
    response_model=MailboxLetterAcceptResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def accept_mailbox_letter(
    letter_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_mailbox_session),
) -> dict:
    result = _mailbox_call(
        lambda: _service().accept_letter(
            platform_user.platform_user_id, letter_id=letter_id, now=_db_now()
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "letter": _item_data(result["letter"]),
            "resident": _resident_data(result["resident"]),
            "replayed": bool(result["replayed"]),
        },
    )


__all__ = ["router"]
