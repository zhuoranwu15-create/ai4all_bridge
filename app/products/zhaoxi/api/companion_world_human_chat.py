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
from app.db import SessionPrincipal
from app.platform.media.access import (
    owner_scope,
    owner_ttl_seconds,
    visit_scope,
    visitor_ttl_seconds,
)
from app.platform.media.persistence import list_media_assets_unscoped
from app.platform.media.view import build_media_content, text_content
from app.products.zhaoxi.application import CompanionWorldHumanChatService, HumanChatError
from app.products.zhaoxi.application.app_display_localization import (
    localized_report_options,
)
from app.products.zhaoxi.api.companion_world import (
    CompanionWorldApiError,
    _envelope,
    _no_store,
    _require_world_session,
    resolve_chat_media,
)
from app.products.zhaoxi.api.contracts import (
    WORLD_ERROR_RESPONSES,
    HumanConversationListResponse,
    HumanReportOptionsResponse,
)
from app.time_utils import beijing_naive_now

router = APIRouter(tags=["companion-world-human-chat"])
_BEIJING_TZ = timezone(timedelta(hours=8))
_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EmptyPayload(BaseModel):
    """拒绝 participant/visit/world/account 注入字段。"""

    model_config = ConfigDict(extra="forbid")


class SendHumanMessagePayload(BaseModel):
    """真人消息发送 payload：文字、媒体，或带 caption 的媒体。"""

    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(min_length=8, max_length=64)
    # v1.5：``text`` 与 ``media_ref`` 至少给一个（图片不带 caption 是常态）。
    # 「两个都空」刻意不在这里拦，而是由端点抛 ``media_content_required``——校验错误统一
    # 收敛成 ``invalid_request``，客户端分不出是格式错还是内容缺失。
    text: str = Field(default="", max_length=4000)
    media_ref: Optional[str] = Field(default=None, max_length=64)

    @field_validator("client_message_id")
    @classmethod
    def _valid_client_id(cls, value: str) -> str:
        if not _CLIENT_MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("invalid client_message_id")
        return value

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("media_ref")
    @classmethod
    def _clean_media_ref(cls, value: Optional[str]) -> Optional[str]:
        return (value or "").strip() or None


class HumanReportPayload(BaseModel):
    """举报正文；block=true 时提交证据后立即拉黑。"""

    model_config = ConfigDict(extra="forbid")

    message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=32)
    details: Optional[str] = Field(default=None, max_length=1000)
    block: bool = False


def _require_human_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
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


def _write_enabled() -> bool:
    """发送门控；只关闭写入，历史/隐藏/拉黑/举报不受影响。"""
    return bool(getattr(settings, "companion_world_human_chat_enabled", False))


def _conversation_data(row: dict) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "visit_id": row["visit_id"],
        "counterpart_display_name": row.get("counterpart_display_name"),
        "status": row["status"],
        "last_message_at": _public_time(row.get("last_message_at")),
        "last_read_at": _public_time(row.get("last_read_at")),
        "created_at": _public_time(row["created_at"]),
        "last_preview": row.get("last_preview"),
        "unread_count": int(row.get("unread_count") or 0),
        "can_send": bool(row.get("can_send")),
        "read_only_reason": row.get("read_only_reason"),
        "expires_at": _public_time(row.get("expires_at")),
    }


def _visit_remaining_seconds(expires_at: Optional[str]) -> Optional[int]:
    """visit 剩余秒数；``expires_at`` 缺失时返回 None（TTL 取配置值）。

    即便这里给宽了，``GET /v1/media/{id}`` 仍会独立复查 visit 状态与剩余时长，
    多签出来的几分钟换不到访问权。
    """
    if not expires_at:
        return None
    try:
        deadline = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int((deadline - _now()).total_seconds())


def _message_content(
    row: dict, platform_user_id: str, *, assets: dict, conversation: dict
) -> dict:
    """按 D-1 输出判别联合；媒体 URL 逐条现签。

    scope 取决于**看的人是不是这条媒体的主人**：自己上传的用 ``pu:<自己>``（TTL 长、可缓存），
    对方发来的只能用 ``visit:<visit_id>``——visit 一结束 URL 立刻失效（§3.3 隐私红线）。
    真人消息的 ``body_text`` 恒为用户自己写的正文（不经 LLM），可直接当 caption。
    """
    caption = row.get("body_text") or ""
    asset = assets.get(str(row.get("media_id") or ""))
    if asset is None:
        return text_content(caption)
    if str(asset.get("owner_platform_user_id") or "") == platform_user_id:
        scope = owner_scope(platform_user_id)
        ttl_seconds = owner_ttl_seconds()
    else:
        scope = visit_scope(str(conversation["visit_id"]))
        ttl_seconds = visitor_ttl_seconds(
            visit_remaining_seconds=_visit_remaining_seconds(
                conversation.get("visit_expires_at")
            )
        )
    return build_media_content(
        asset=asset, caption=caption, scope=scope, ttl_seconds=ttl_seconds
    )


def _message_data(
    row: dict, platform_user_id: str, *, assets: dict, conversation: dict
) -> dict:
    """消息 sender 只公开 self/counterpart，不返回内部 user id。"""
    return {
        "message_id": row["id"],
        "sender": (
            "self"
            if row["sender_platform_user_id"] == platform_user_id
            else "counterpart"
        ),
        "content": _message_content(
            row, platform_user_id, assets=assets, conversation=conversation
        ),
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


@router.get(
    "/human-conversations",
    response_model=HumanConversationListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def list_human_conversations(
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    rows = _service().list_conversations(
        platform_user.platform_user_id, now=_now(), write_enabled=_write_enabled()
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"items": [_conversation_data(row) for row in rows]},
    )


@router.get(
    "/human-conversations/report-options",
    response_model=HumanReportOptionsResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def list_human_report_options(
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    """举报原因受控表；随读门控开放，`human_chat_send=false` 时仍可举报。"""
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data=localized_report_options(_service().report_options()),
    )


@router.get("/human-conversations/{conversation_id}/messages")
def list_human_messages(
    conversation_id: str,
    request: Request,
    response: Response,
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    cursor_sequence_no, cursor_message_id = _decode_cursor(cursor)
    conversation, rows = _call(
        lambda: _service().list_messages(
            platform_user.platform_user_id,
            conversation_id=conversation_id,
            now=_now(),
            cursor_sequence_no=cursor_sequence_no,
            cursor_message_id=cursor_message_id,
            limit=limit + 1,
        )
    )
    page = rows[:limit]
    # 一次批量取本页涉及的资产，避免逐条查库。
    assets = list_media_assets_unscoped(
        media_ids=[str(row["media_id"]) for row in page if row.get("media_id")]
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [
                _message_data(
                    row,
                    platform_user.platform_user_id,
                    assets=assets,
                    conversation=conversation,
                )
                for row in page
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
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    if payload.media_ref:
        # owner 锚定与门控在这里完成；service 只负责在发送事务内认领资产。
        asset = resolve_chat_media(
            media_ref=payload.media_ref,
            platform_user_id=platform_user.platform_user_id,
        )
    elif not payload.text:
        raise CompanionWorldApiError("media_content_required")
    else:
        asset = None
    result = _call(
        lambda: _service().send(
            platform_user.platform_user_id,
            conversation_id=conversation_id,
            client_message_id=payload.client_message_id,
            body_text=payload.text,
            now=_now(),
            write_enabled=_write_enabled(),
            media_id=None if asset is None else str(asset["id"]),
        )
    )
    _no_store(response)
    message = result["message"]
    # 回执里的媒体恒属于发送者本人，直接用 owner scope 签；无需再查 visit。
    assets = {} if asset is None else {str(asset["id"]): asset}
    return _envelope(
        request,
        code="ok",
        data={
            "message": _message_data(
                message,
                platform_user.platform_user_id,
                assets=assets,
                conversation={"visit_id": "", "visit_expires_at": None},
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
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    row = _call(
        lambda: _service().mark_read(
            platform_user.platform_user_id, conversation_id=conversation_id, now=_now()
        )
    )
    _no_store(response)
    is_owner = row["owner_platform_user_id"] == platform_user.platform_user_id
    read_at = row["owner_last_read_at" if is_owner else "visitor_last_read_at"]
    return _envelope(request, code="ok", data={"read_at": _public_time(read_at)})


@router.delete("/human-conversations/{conversation_id}/entry")
def hide_human_conversation(
    conversation_id: str,
    request: Request,
    response: Response,
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    _call(
        lambda: _service().hide(
            platform_user.platform_user_id, conversation_id=conversation_id, now=_now()
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
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    result = _call(
        lambda: _service().report(
            platform_user.platform_user_id,
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
    platform_user: SessionPrincipal = Depends(_require_human_session),
) -> dict:
    result = _call(
        lambda: _service().block(
            platform_user.platform_user_id, conversation_id=conversation_id, now=_now()
        )
    )
    _no_store(response)
    return _envelope(request, code="ok", data=result)


__all__ = ["router"]
