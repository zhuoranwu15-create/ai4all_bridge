"""Companion World v1.5 异步居民许愿 owner API。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import SessionPrincipal
from app.products.mingchan.api.world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
)
from app.products.mingchan.api.world_contracts import (
    WORLD_ERROR_RESPONSES,
    ResidentWishCurrentResponse,
    ResidentWishSubmitResponse,
    ResidentWishWithdrawResponse,
)
from app.products.mingchan.application.resident_wishes import (
    CompanionWorldResidentWishService,
    ResidentWishError,
)
from app.products.mingchan.application.wish import MAX_WISH_TEXT_CHARS
from app.time_utils import beijing_naive_now

router = APIRouter(tags=["companion-world-resident-wishes"])

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


class SubmitResidentWishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wish_text: str = Field(min_length=1, max_length=MAX_WISH_TEXT_CHARS)
    client_request_id: str = Field(min_length=8, max_length=128, pattern=_REQUEST_ID_RE.pattern)


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_wish_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "mingchan_mailbox_enabled", False)):
        raise CompanionWorldApiError("feature_disabled", 404)
    return _require_world_session(authorization)


def _service() -> CompanionWorldResidentWishService:
    return CompanionWorldResidentWishService()


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_BEIJING_TZ
    ).isoformat()


def _wish_data(item) -> Optional[dict]:
    if item is None:
        return None
    return {
        "wish_id": item.wish_id,
        "status": item.status,
        "submitted_at": _public_time(item.submitted_at),
        "expected_delivery_from": _public_time(item.expected_delivery_from),
        "expected_delivery_to": _public_time(item.expected_delivery_to),
        "letter_id": item.letter_id,
        "is_open": item.is_open,
        "can_withdraw": item.can_withdraw,
        "terminal_reason": item.terminal_reason,
    }


def _wish_call(action):
    try:
        return action()
    except ResidentWishError as err:
        raise CompanionWorldApiError(err.code) from err


@router.post(
    "/worlds/home/resident-wishes",
    status_code=202,
    response_model=ResidentWishSubmitResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def submit_resident_wish(
    payload: SubmitResidentWishPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_wish_session),
) -> dict:
    item, replayed = _wish_call(
        lambda: _service().submit(
            principal.platform_user_id,
            wish_text=payload.wish_text,
            client_request_id=payload.client_request_id,
            now=beijing_naive_now(),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"wish": _wish_data(item), "replayed": replayed},
    )


@router.get(
    "/worlds/home/resident-wishes/current",
    response_model=ResidentWishCurrentResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def current_resident_wish(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_wish_session),
) -> dict:
    item = _wish_call(
        lambda: _service().current(
            principal.platform_user_id,
            now=beijing_naive_now(),
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"wish": _wish_data(item)})


@router.post(
    "/resident-wishes/{wish_id}/withdraw",
    response_model=ResidentWishWithdrawResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def withdraw_resident_wish_endpoint(
    wish_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    principal: SessionPrincipal = Depends(_require_wish_session),
) -> dict:
    item, replayed = _wish_call(
        lambda: _service().withdraw(
            principal.platform_user_id,
            wish_id=wish_id,
            now=beijing_naive_now(),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"wish": _wish_data(item), "replayed": replayed},
    )


__all__ = ["router"]
