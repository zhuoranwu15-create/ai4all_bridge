"""Companion World M5 invite/pending/active visit owner/visitor API。"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.db import SessionPrincipal
from app.products.zhaoxi.application import CompanionWorldVisitService, VisitError
from app.platform.quota.rate_limiter import RateLimiter
from app.products.zhaoxi.api.companion_world import (
    CompanionWorldApiError,
    _envelope,
    _decode_feed_cursor,
    _no_store,
    _require_world_session,
)
from app.time_utils import beijing_naive_now

router = APIRouter(tags=["companion-world-visits"])
_BEIJING_TZ = timezone(timedelta(hours=8))
_REDEEM_USER_RPM = 10
_REDEEM_IP_RPM = 30
_redeem_rate_limiter = RateLimiter()


class EmptyPayload(BaseModel):
    """拒绝 owner/visitor/universe/account 注入字段。"""

    model_config = ConfigDict(extra="forbid")


class RedeemVisitPayload(BaseModel):
    """认证后兑换的一次性 code；大小写敏感。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)

    @field_validator("code")
    @classmethod
    def _clean_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("code is required")
        return cleaned


def _require_visit_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "companion_world_visits_enabled", False)):
        raise CompanionWorldApiError("feature_disabled", 404)
    return _require_world_session(authorization)


def _service() -> CompanionWorldVisitService:
    return CompanionWorldVisitService()


def _now() -> datetime:
    return beijing_naive_now().replace(microsecond=0)


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_BEIJING_TZ).isoformat()


def _invite_data(row: dict, *, code: Optional[str] = None) -> dict:
    """白名单序列化 invite；hash/owner/world/redeemer 永不公开。"""
    item = {
        "invite_id": row["id"],
        "code_prefix": row["code_prefix"],
        "status": row["status"],
        "expires_at": _public_time(row["expires_at"]),
        "created_at": _public_time(row["created_at"]),
        "redeemed_at": _public_time(row.get("redeemed_at")),
    }
    if code is not None:
        item["code"] = code
    return item


def _visit_data(row: dict, platform_user_id: str) -> dict:
    """白名单序列化 visit；不暴露双方内部 user id 或 universe id。"""
    if "role" in row:
        role = row["role"]
        counterpart = row.get("counterpart_display_name")
    else:
        role = (
            "owner"
            if row["owner_platform_user_id"] == platform_user_id
            else "visitor"
        )
        counterpart = row.get(
            "visitor_display_name" if role == "owner" else "owner_display_name"
        )
    return {
        "visit_id": row["visit_id"] if "visit_id" in row else row["id"],
        "role": role,
        "counterpart_display_name": counterpart,
        "status": row["status"],
        "pending_expires_at": _public_time(row.get("pending_expires_at")),
        "accepted_at": _public_time(row.get("accepted_at")),
        "expires_at": _public_time(row.get("expires_at")),
        "terminal_at": _public_time(row.get("terminal_at")),
        "terminal_reason": row.get("terminal_reason"),
        "created_at": _public_time(row["created_at"]),
    }


def _feed_data(row: dict) -> dict:
    """复用 M3 Feed 公开形状，不返回 owner/world/runtime/fingerprint。"""
    return {
        "post_id": row["id"],
        "author": {
            "type": row["author_type"],
            "resident_id": row.get("author_resident_id"),
            "name": row.get("author_name"),
            "avatar_ref": row.get("author_avatar_ref"),
        },
        "content": {"type": "text", "text": row.get("text")},
        "post_type": row.get("post_type") or "normal",
        "source": row["source_type"],
        "published_at": _public_time(row.get("published_at")),
    }


def _encode_feed_cursor(row: dict) -> str:
    payload = json.dumps(
        {"v": 1, "published_at": row["published_at"], "id": row["id"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _call(action):
    try:
        return action()
    except VisitError as err:
        raise CompanionWorldApiError(err.code) from err


@router.post("/world/invites")
def create_world_invite(
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    result = _call(
        lambda: _service().create_invite(platform_user.platform_user_id, now=_now())
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"invite": _invite_data(result["invite"], code=result["code"])},
    )


@router.get("/world/invites")
def list_world_invites(
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    rows = _service().list_invites_current(
        platform_user.platform_user_id, now=_now()
    )
    _no_store(response)
    return _envelope(
        request, code="ok", data={"items": [_invite_data(row) for row in rows]}
    )


@router.delete("/world/invites/{invite_id}")
def revoke_world_invite(
    invite_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    row = _call(
        lambda: _service().revoke_invite(
            platform_user.platform_user_id, invite_id=invite_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"invite": _invite_data(row)})


@router.post("/visits/redeem")
def redeem_world_invite(
    payload: RedeemVisitPayload,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    platform_user_id = platform_user.platform_user_id
    client_host = request.client.host if request.client else "unknown"
    if not _redeem_rate_limiter.check_rpm(
        f"world-invite-redeem:user:{platform_user_id}",
        _REDEEM_USER_RPM,
        window_seconds=60.0,
    ) or not _redeem_rate_limiter.check_rpm(
        f"world-invite-redeem:ip:{client_host}",
        _REDEEM_IP_RPM,
        window_seconds=60.0,
    ):
        raise CompanionWorldApiError("rate_limited")
    row = _call(
        lambda: _service().redeem(
            platform_user_id, code=payload.code, now=_now()
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"visit": _visit_data(row, platform_user_id)},
    )


@router.get("/visits")
def list_world_visits(
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    rows = _service().list_visits_current(
        platform_user.platform_user_id, now=_now()
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [
                _visit_data(row, platform_user.platform_user_id) for row in rows
            ]
        },
    )


def _accept_response(
    *,
    visit_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal,
) -> dict:
    result = _call(
        lambda: _service().accept(
            platform_user.platform_user_id, visit_id=visit_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "visit": _visit_data(result["visit"], platform_user.platform_user_id),
            "human_conversation_id": result["conversation"]["id"],
            "replayed": bool(result["replayed"]),
        },
    )


@router.post("/visits/{visit_id}/accept")
def accept_world_visit(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    return _accept_response(
        visit_id=visit_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


def _terminate_response(
    *,
    action: str,
    visit_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal,
) -> dict:
    row = _call(
        lambda: _service().terminate(
            platform_user.platform_user_id,
            visit_id=visit_id,
            action=action,
            now=_now(),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"visit": _visit_data(row, platform_user.platform_user_id)},
    )


@router.post("/visits/{visit_id}/reject")
def reject_world_visit(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    return _terminate_response(
        action="reject",
        visit_id=visit_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post("/visits/{visit_id}/cancel")
def cancel_world_visit(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    return _terminate_response(
        action="cancel",
        visit_id=visit_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post("/visits/{visit_id}/leave")
def leave_world_visit(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    return _terminate_response(
        action="leave",
        visit_id=visit_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.post("/visits/{visit_id}/revoke")
def revoke_world_visit(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    return _terminate_response(
        action="revoke",
        visit_id=visit_id,
        request=request,
        response=response,
        platform_user=platform_user,
    )


@router.get("/visits/{visit_id}/feed")
def list_visited_world_feed(
    visit_id: str,
    request: Request,
    response: Response,
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    cursor_published_at, cursor_post_id = _decode_feed_cursor(cursor)
    rows = _call(
        lambda: _service().list_feed(
            platform_user.platform_user_id,
            visit_id=visit_id,
            now=_now(),
            cursor_published_at=cursor_published_at,
            cursor_post_id=cursor_post_id,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [_feed_data(row) for row in page],
            "next_cursor": (
                _encode_feed_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.post("/visits/{visit_id}/block")
def block_visit_counterpart(
    visit_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: SessionPrincipal = Depends(_require_visit_session),
) -> dict:
    result = _call(
        lambda: _service().block(
            platform_user.platform_user_id, visit_id=visit_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data=result)


__all__ = ["router"]
