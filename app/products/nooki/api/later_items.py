"""Nooki 稍后盒子 API：服务端权威 CRUD，不经过 LLM。"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import SessionPrincipal
from app.products.nooki.infrastructure.repositories.later_items import (
    LaterItemNotFoundError,
    LaterItemVersionConflictError,
    NookiLaterItemRepository,
)
from app.routers.deps import require_product_session

router = APIRouter(tags=["nooki-later-items"])
_require_nooki_session = require_product_session(NOOKI_APP_ID)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class CreateLaterItemRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    client_request_id: str = Field(min_length=8, max_length=64)
    source_message_id: Optional[str] = Field(default=None, max_length=128)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be blank")
        return cleaned

    @field_validator("client_request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("invalid client_request_id")
        return value


class UpdateLaterItemRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be blank")
        return cleaned


class ArchiveLaterItemRequest(BaseModel):
    expected_version: int = Field(ge=1)


def _write_error(err: Exception) -> HTTPException:
    if isinstance(err, LaterItemVersionConflictError):
        return HTTPException(status_code=409, detail="later_item_version_conflict")
    return HTTPException(status_code=404, detail="later_item_not_found")


@router.get("/later-items")
def list_later_items(
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    items = NookiLaterItemRepository().list_items(
        platform_user_id=principal.platform_user_id
    )
    _no_store(response)
    return {"status": "ok", "items": items}


@router.post("/later-items")
def create_later_item(
    payload: CreateLaterItemRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    item, deduplicated = NookiLaterItemRepository().create(
        platform_user_id=principal.platform_user_id,
        content=payload.content,
        client_request_id=payload.client_request_id,
        source_message_id=payload.source_message_id,
    )
    _no_store(response)
    return {
        "status": "ok",
        "item": item,
        "metadata": {"deduplicated": deduplicated},
    }


@router.patch("/later-items/{item_id}")
def update_later_item(
    item_id: str,
    payload: UpdateLaterItemRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    try:
        item = NookiLaterItemRepository().update_content(
            platform_user_id=principal.platform_user_id,
            item_id=item_id,
            content=payload.content,
            expected_version=payload.expected_version,
        )
    except (LaterItemNotFoundError, LaterItemVersionConflictError) as err:
        raise _write_error(err) from err
    _no_store(response)
    return {"status": "ok", "item": item}


@router.post("/later-items/{item_id}/archive")
def archive_later_item(
    item_id: str,
    payload: ArchiveLaterItemRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    try:
        item = NookiLaterItemRepository().archive(
            platform_user_id=principal.platform_user_id,
            item_id=item_id,
            expected_version=payload.expected_version,
        )
    except (LaterItemNotFoundError, LaterItemVersionConflictError) as err:
        raise _write_error(err) from err
    _no_store(response)
    return {"status": "ok", "item": item}


__all__ = ["router"]
