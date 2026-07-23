"""Companion World M5 独立真人聊天 participant API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.platform import CompanionWorldHumanChatService, HumanChatError
from app.routers.companion_world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
)
from app.time_utils import beijing_naive_now

router = APIRouter(prefix="/v1", tags=["companion-world-human-chat"])
_BEIJING_TZ = timezone(timedelta(hours=8))
_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EmptyPayload(BaseModel):
    """拒绝 participant/visit/world/account 注入字段。"""

    model_config = ConfigDict(extra="forbid")


class SendHumanMessagePayload(BaseModel):
    """真人纯文字发送 payload。"""

    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("client_message_id")
    @classmethod
    def _valid_client_id(cls, value: str) -> str:
        if not _CLIENT_MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("invalid client_message_id")
        return value

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text is required")
        return cleaned


class HumanReportPayload(BaseModel):
    """举报正文；block=true 时提交证据后立即拉黑。"""

    model_config = ConfigDict(extra="forbid")

    message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=32)
    details: Optional[str] = Field(default=None, max_length=1000)
    block: bool = False


def _require_human_session(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    return _require_world_session(authorization)


def _service() -> CompanionWorldHumanChatService:
    return CompanionWorldHumanChatService()


def _now() -> datetime:
    return beijing_naive_now().replace(microsecond=0)


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_BEIJING_TZ).isoformat()


def _conversation_data(row: dict) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "visit_id": row["visit_id"],
        "counterpart_display_name": row.get("counterpart_display_name"),
        "status": row["status"],
        "last_message_at": _public_time(row.get("last_message_at")),
        "last_read_at": _public_time(row.get("last_read_at")),
        "created_at": _public_time(row["created_at"]),
    }


def _message_data(row: dict, platform_user_id: str) -> dict:
    """消息 sender 只公开 self/counterpart，不返回内部 user id。"""
    return {
        "message_id": row["id"],
        "sender": (
            "self"
            if row["sender_platform_user_id"] == platform_user_id
            else "counterpart"
        ),
        "content": {"type": "text", "text": row["body_text"]},
        "sequence": int(row["sequence_no"]),
        "created_at": _public_time(row["created_at"]),
    }


def _encode_cursor(row: dict) -> str:
    payload = json.dumps(
        {"v": 1, "sequence": int(row["sequence_no"]), "id": row["id"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    if cursor is None:
        return None, None
    try:
        clean = cursor.strip()
        raw = base64.b64decode(
            (clean + "=" * (-len(clean) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"v", "sequence", "id"}:
            raise ValueError("invalid cursor")
        if value["v"] != 1:
            raise ValueError("invalid cursor version")
        sequence_no = int(value["sequence"])
        message_id = str(value["id"])
        if sequence_no < 1 or not _CURSOR_ID_RE.fullmatch(message_id):
            raise ValueError("invalid cursor id")
        return sequence_no, message_id
    except (UnicodeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise CompanionWorldApiError("invalid_cursor")


def _call(action):
    try:
        return action()
    except HumanChatError as err:
        raise CompanionWorldApiError(err.code) from err


@router.get("/human-conversations")
def list_human_conversations(
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    rows = _service().list_conversations(str(platform_user["id"]), now=_now())
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"items": [_conversation_data(row) for row in rows]},
    )


@router.get("/human-conversations/{conversation_id}/messages")
def list_human_messages(
    conversation_id: str,
    request: Request,
    response: Response,
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    cursor_sequence_no, cursor_message_id = _decode_cursor(cursor)
    rows = _call(
        lambda: _service().list_messages(
            str(platform_user["id"]),
            conversation_id=conversation_id,
            now=_now(),
            cursor_sequence_no=cursor_sequence_no,
            cursor_message_id=cursor_message_id,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [
                _message_data(row, str(platform_user["id"])) for row in page
            ],
            "next_cursor": (
                _encode_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.post("/human-conversations/{conversation_id}/messages")
def send_human_message(
    conversation_id: str,
    payload: SendHumanMessagePayload,
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    result = _call(
        lambda: _service().send(
            str(platform_user["id"]),
            conversation_id=conversation_id,
            client_message_id=payload.client_message_id,
            body_text=payload.text,
            now=_now(),
            write_enabled=bool(
                getattr(settings, "companion_world_human_chat_enabled", False)
            ),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "message": _message_data(
                result["message"], str(platform_user["id"])
            ),
            "created": bool(result["created"]),
        },
    )


@router.post("/human-conversations/{conversation_id}/read")
def read_human_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    row = _call(
        lambda: _service().mark_read(
            str(platform_user["id"]), conversation_id=conversation_id, now=_now()
        )
    )
    _no_store(response)
    is_owner = row["owner_platform_user_id"] == str(platform_user["id"])
    read_at = row["owner_last_read_at" if is_owner else "visitor_last_read_at"]
    return _envelope(request, code="ok", data={"read_at": _public_time(read_at)})


@router.delete("/human-conversations/{conversation_id}/entry")
def hide_human_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    _call(
        lambda: _service().hide(
            str(platform_user["id"]), conversation_id=conversation_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data={"hidden": True})


@router.post("/human-conversations/{conversation_id}/report")
def report_human_conversation(
    conversation_id: str,
    payload: HumanReportPayload,
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    result = _call(
        lambda: _service().report(
            str(platform_user["id"]),
            conversation_id=conversation_id,
            message_id=payload.message_id,
            reason_code=payload.reason_code,
            details_text=payload.details,
            block_after=payload.block,
            now=_now(),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "report_id": result["report"]["id"],
            "status": result["report"]["status"],
            "created_at": _public_time(result["report"]["created_at"]),
            "blocked": result["block"] is not None,
        },
    )


@router.post("/human-conversations/{conversation_id}/block")
def block_human_counterpart(
    conversation_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyPayload] = None,
    platform_user: dict = Depends(_require_human_session),
) -> dict:
    result = _call(
        lambda: _service().block(
            str(platform_user["id"]), conversation_id=conversation_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data=result)


__all__ = ["router"]
