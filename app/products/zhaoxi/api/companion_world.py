"""Companion World P1 控制面与 M3 文字 Feed owner API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.db import (
    SessionPrincipal,
    get_duplicate_reply,
    get_platform_user,
    try_conversation_turn_lock,
)
from app.products.zhaoxi.domain.companion_world import (
    CandidateRecord,
    CompanionWorldError,
    CompanionWorldFeedService,
    CompanionWorldService,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
)
from app.products.zhaoxi.application import (
    SqlCompanionWorldRepository,
    run_companion_world_turn,
)
from app.time_utils import beijing_now
from app.routers.deps import _resolve_legacy_session_principal

router = APIRouter(prefix="/v1", tags=["companion-world"])

_ERROR_STATUS = {
    "not_found": 404,
    "unauthorized": 401,
    "account_id_not_accepted": 400,
    "world_not_ready": 409,
    "world_disabled": 403,
    "template_not_found": 404,
    "template_not_available": 409,
    "preset_catalog_not_ready": 503,
    "resident_not_found": 404,
    "resident_capacity_exceeded": 409,
    "resident_capacity_empty": 409,
    "resident_selection_invalid": 400,
    "resident_already_exists": 409,
    "custom_candidate_limit_exceeded": 409,
    "conversation_not_found": 404,
    "conversation_read_only": 409,
    "turn_in_progress": 409,
    "rate_limited": 429,
    "account_disabled": 403,
    "invalid_cursor": 400,
    "idempotency_conflict": 409,
    "notification_not_found": 404,
    "letter_not_found": 404,
    "letter_not_open": 409,
    "letter_expired": 409,
    "letter_template_unavailable": 409,
    "invalid_invite_code": 400,
    "self_invite_not_allowed": 403,
    "visit_contact_blocked": 403,
    "invite_not_found": 404,
    "visit_not_found": 404,
    "invite_expired": 409,
    "invite_unavailable": 409,
    "visitor_visit_limit_reached": 409,
    "world_visit_limit_reached": 409,
    "visit_already_open": 409,
    "visit_pending_expired": 409,
    "visit_not_pending": 409,
    "visit_not_active": 409,
    "human_conversation_not_found": 404,
    "human_message_not_found": 404,
    "human_chat_read_only": 409,
    "invalid_request": 422,
}

_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_FEED_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_FEED_CURSOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BEIJING_TZ = timezone(timedelta(hours=8))


class CompanionWorldApiError(Exception):
    """仅供新世界端点使用的稳定 HTTP 错误。"""

    def __init__(self, code: str, status_code: Optional[int] = None) -> None:
        self.code = code
        self.status_code = status_code or _ERROR_STATUS.get(code, 500)
        super().__init__(code)


class SelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=50)


class ConfirmResidentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[SelectionPayload] = Field(max_length=10)


class CreateResidentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    persona_hint: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CreateResidentPayload":
        if bool(self.template_id) == bool(self.name):
            raise ValueError("provide exactly one of template_id or name")
        if self.template_id and self.persona_hint:
            raise ValueError("persona_hint requires name")
        return self


class ConversationTurnPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _clean_turn(self) -> "ConversationTurnPayload":
        self.text = self.text.strip()
        if not self.text or not _CLIENT_MESSAGE_ID_RE.fullmatch(self.client_message_id):
            raise ValueError("invalid turn payload")
        return self


class FeedPostPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=8, max_length=128)
    text: str

    @model_validator(mode="after")
    def _clean_feed_post(self) -> "FeedPostPayload":
        self.client_request_id = self.client_request_id.strip()
        self.text = self.text.strip()
        if not _FEED_CLIENT_REQUEST_ID_RE.fullmatch(self.client_request_id):
            raise ValueError("invalid client_request_id")
        if not self.text or len(self.text) > 2000:
            raise ValueError("invalid feed text")
        return self


def _request_id(request: Request) -> str:
    current = getattr(request.state, "companion_world_request_id", None)
    if current:
        return str(current)
    value = f"req_{uuid.uuid4().hex}"
    request.state.companion_world_request_id = value
    return value


def _envelope(request: Request, *, code: str, data=None) -> dict:
    payload = {
        "code": code,
        "request_id": _request_id(request),
        "server_time": beijing_now().isoformat(timespec="seconds"),
    }
    if code == "ok":
        payload["data"] = data
    else:
        payload["message"] = None
    return payload


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _require_world_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "companion_world_p1_enabled", False)):
        raise CompanionWorldApiError("not_found", 404)
    if not authorization or not authorization.startswith("Bearer "):
        raise CompanionWorldApiError("unauthorized", 401)
    principal = _resolve_legacy_session_principal(authorization)
    if principal is None:
        raise CompanionWorldApiError("unauthorized", 401)
    return principal


def _require_feed_session(
    authorization: Optional[str] = Header(default=None),
) -> SessionPrincipal:
    if not bool(getattr(settings, "companion_world_feed_enabled", False)):
        raise CompanionWorldApiError("not_found", 404)
    return _require_world_session(authorization)


def _service() -> CompanionWorldService:
    return CompanionWorldService(SqlCompanionWorldRepository())


def _feed_service() -> CompanionWorldFeedService:
    return CompanionWorldFeedService(SqlCompanionWorldRepository())


def _run_domain(action: Callable):
    try:
        return action()
    except CompanionWorldError as err:
        raise CompanionWorldApiError(err.code) from err


def _candidate_data(candidate: CandidateRecord) -> dict:
    """序列化候选公开字段；刻意不含 persona_seed_json 和内部 resident id。"""
    return {
        "template_id": candidate.template.id,
        "template_version": candidate.template_version,
        "name": candidate.template.name,
        "avatar_ref": candidate.template.avatar_ref,
        "summary": candidate.template.summary,
        "tags": list(candidate.template.tags),
        "origin": candidate.origin,
        "status": candidate.status,
    }


def _resident_data(resident: ResidentRecord) -> dict:
    """序列化 owner 可见居民字段；不暴露 runtime account id。"""
    return {
        "resident_id": resident.resident_id,
        "name": resident.name,
        "avatar_ref": resident.avatar_ref,
        "status": resident.status,
        "origin": resident.origin,
        "conversation_id": resident.conversation_id,
        "conversation_state": resident.conversation_state,
    }


def _conversation_data(item) -> dict:
    return {
        "conversation_id": item.conversation_id,
        "resident": {
            "id": item.resident_id,
            "name": item.resident_name,
            "avatar_ref": item.resident_avatar_ref,
            "status": item.resident_status,
        },
        "state": item.state,
        "last_preview": item.last_preview,
        "unread": item.unread,
    }


def _feed_time(value: Optional[str]) -> Optional[str]:
    """把 DB 北京 naive 时间转成带 +08:00 的公开 ISO 时间。"""
    if not value:
        return None
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_BEIJING_TZ).isoformat()


def _feed_item_data(item) -> dict:
    """序列化 Feed 公开 DTO，不泄漏 owner/runtime/fingerprint/outbox 字段。"""
    return {
        "post_id": item.id,
        "author": {
            "type": item.author_type,
            "resident_id": item.author_resident_id,
            "name": item.author_name,
            "avatar_ref": item.author_avatar_ref,
        },
        "content": {"type": "text", "text": item.text},
        "post_type": item.post_type,
        "source": item.source_type,
        "published_at": _feed_time(item.published_at),
    }


def _encode_feed_cursor(item) -> str:
    payload = json.dumps(
        {"v": 1, "published_at": item.published_at, "id": item.id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_feed_cursor(cursor: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if cursor is None:
        return None, None
    clean = cursor.strip()
    if not clean:
        raise CompanionWorldApiError("invalid_cursor")
    try:
        padded = clean + "=" * (-len(clean) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"v", "published_at", "id"}:
            raise ValueError("invalid cursor shape")
        if value["v"] != 1:
            raise ValueError("invalid cursor version")
        published_at = str(value["published_at"])
        post_id = str(value["id"])
        datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S")
        if not _FEED_CURSOR_ID_RE.fullmatch(post_id):
            raise ValueError("invalid cursor id")
    except (UnicodeError, ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        raise CompanionWorldApiError("invalid_cursor")
    return published_at, post_id


@router.post("/worlds/home/bootstrap")
def bootstrap_home(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    result = _run_domain(lambda: _service().bootstrap_home(principal.platform_user_id))
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "world": {
                "id": result.world.id,
                "status": result.world.status,
                "onboarding_state": result.world.onboarding_state,
            },
            "candidates": [_candidate_data(item) for item in result.candidates],
        },
    )


@router.get("/worlds/home/resident-candidates")
def list_candidates(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    candidates = _run_domain(
        lambda: _service().list_candidates(principal.platform_user_id)
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"candidates": [_candidate_data(item) for item in candidates]},
    )


@router.post("/worlds/home/residents/confirm")
def confirm_residents(
    payload: ConfirmResidentsPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    residents = _run_domain(
        lambda: _service().confirm_residents(
            principal.platform_user_id,
            [
                ResidentSelection(
                    template_id=item.template_id,
                    display_name=item.display_name,
                )
                for item in payload.selections
            ],
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"residents": [_resident_data(item) for item in residents]},
    )


@router.get("/worlds/home/residents")
def list_residents(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    residents = _run_domain(
        lambda: _service().list_residents(principal.platform_user_id)
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"residents": [_resident_data(item) for item in residents]},
    )


@router.post("/worlds/home/residents")
def create_resident(
    payload: CreateResidentPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    custom = None
    if payload.name:
        name = payload.name.strip()
        hint = (payload.persona_hint or "").strip()
        soul = hint or f"你是{name}，是用户世界里一位独立、自然的 AI 陪伴。"
        custom = TemplateDraft(
            name=name,
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n{soul}",
                    "IDENTITY.md": (
                        f"# IDENTITY\n\n- 你的名字是 {name}，用它自称。\n"
                        "- 你是用户的个人 AI 陪伴。"
                    ),
                },
                ensure_ascii=False,
            ),
        )
    result = _run_domain(
        lambda: _service().create_resident(
            principal.platform_user_id,
            template_id=payload.template_id,
            custom_template=custom,
        )
    )
    _no_store(response)
    if isinstance(result, CandidateRecord):
        data = {"candidate": _candidate_data(result)}
    else:
        data = {"resident": _resident_data(result)}
    return _envelope(request, code="ok", data=data)


@router.get("/worlds/home/feed")
def list_home_feed(
    request: Request,
    response: Response,
    cursor: Optional[str] = Query(default=None, max_length=1024),
    limit: int = Query(default=20, ge=1, le=50),
    principal: SessionPrincipal = Depends(_require_feed_session),
) -> dict:
    cursor_published_at, cursor_post_id = _decode_feed_cursor(cursor)
    rows = _run_domain(
        lambda: _feed_service().list_published_posts(
            principal.platform_user_id,
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
            "items": [_feed_item_data(item) for item in page],
            "next_cursor": (
                _encode_feed_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.post("/worlds/home/feed/posts")
def publish_home_feed_post(
    payload: FeedPostPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_feed_session),
) -> dict:
    post, created = _run_domain(
        lambda: _feed_service().publish_user_post(
            principal.platform_user_id,
            client_request_id=payload.client_request_id,
            text=payload.text,
            published_at=beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    response.status_code = 201 if created else 200
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"post": _feed_item_data(post)},
    )


@router.get("/conversations")
def list_conversations(
    request: Request,
    response: Response,
    cursor: Optional[str] = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    items = _run_domain(
        lambda: _service().list_conversations(
            principal.platform_user_id,
            cursor_conversation_id=cursor,
            limit=limit,
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [_conversation_data(item) for item in items],
            "next_cursor": items[-1].conversation_id if len(items) == limit else None,
        },
    )


@router.get("/ai-conversations/{conversation_id}/messages")
def list_conversation_messages(
    conversation_id: str,
    request: Request,
    response: Response,
    cursor: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    target, messages = _run_domain(
        lambda: _service().list_conversation_messages(
            principal.platform_user_id,
            conversation_id,
            before_id=cursor,
            limit=limit,
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "state": target.state,
            "messages": [
                {
                    "id": item.id,
                    "message_id": item.message_id,
                    "role": item.role,
                    "message_type": item.message_type,
                    "text": item.content,
                    "created_at": item.created_at,
                }
                for item in messages
            ],
            "next_cursor": messages[0].id if len(messages) == limit else None,
        },
    )


@router.post("/ai-conversations/{conversation_id}/turn")
def conversation_turn(
    conversation_id: str,
    payload: ConversationTurnPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    service = _service()
    target = _run_domain(
        lambda: service.resolve_conversation(
            principal.platform_user_id, conversation_id
        )
    )
    # 用客户端已知的 conversation 锚定幂等键，避免内部 runtime account 出现在历史响应。
    mapped_message_id = f"app:{target.conversation_id}:{payload.client_message_id}"
    duplicate = get_duplicate_reply(
        account_id=target.runtime_account_id,
        reply_to_message_id=mapped_message_id,
    )
    if duplicate is not None:
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={
                "reply": {"text": duplicate, "message_id": None},
                "no_reply": False,
                "deduplicated": True,
            },
        )

    with try_conversation_turn_lock(conversation_id) as acquired:
        if not acquired:
            raise CompanionWorldApiError("turn_in_progress")
        # L2 锁内重读 state，确保未来 offline(M4) 与 turn 以同一 conversation 锁串行。
        target = _run_domain(
            lambda: service.resolve_conversation(
                principal.platform_user_id, conversation_id
            )
        )
        if target.state != "active":
            raise CompanionWorldApiError("conversation_read_only")
        duplicate = get_duplicate_reply(
            account_id=target.runtime_account_id,
            reply_to_message_id=mapped_message_id,
        )
        if duplicate is not None:
            result = None
        else:
            platform_user = get_platform_user(
                platform_user_id=principal.platform_user_id
            ) or {}
            result = run_companion_world_turn(
                conversation_id=target.conversation_id,
                universe_id=target.universe_id,
                resident_id=target.resident_id,
                runtime_account_id=target.runtime_account_id,
                platform_user_id=principal.platform_user_id,
                sender_name=platform_user.get("display_name"),
                message_id=mapped_message_id,
                text=payload.text,
            )

    if result is None:
        reply, reply_message_id, deduplicated, no_reply = duplicate, None, True, False
    else:
        if result.status == "rate_limited":
            raise CompanionWorldApiError("rate_limited")
        if result.status == "disabled":
            raise CompanionWorldApiError("account_disabled")
        reply = result.reply
        reply_message_id = result.metadata.get("reply_message_id")
        deduplicated = result.status == "duplicate"
        no_reply = result.no_reply
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "reply": {"text": reply, "message_id": reply_message_id},
            "no_reply": no_reply,
            "deduplicated": deduplicated,
        },
    )


async def _api_error_handler(request: Request, exc: CompanionWorldApiError):
    response = JSONResponse(
        status_code=exc.status_code,
        content=_envelope(request, code=exc.code),
    )
    _no_store(response)
    return response


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    companion_path = request.url.path.startswith(
        (
            "/v1/worlds/",
            "/v1/conversations",
            "/v1/ai-conversations/",
            "/v1/notifications",
            "/v1/world/invites",
            "/v1/visits",
            "/v1/human-conversations",
        )
    )
    if companion_path:
        forbidden_account_id = isinstance(exc.body, dict) and bool(
            {"account_id", "runtime_account_id", "universe_id"}.intersection(exc.body)
        )
        code = "account_id_not_accepted" if forbidden_account_id else "invalid_request"
        response = JSONResponse(
            status_code=400 if forbidden_account_id else 422,
            content=_envelope(request, code=code),
        )
        _no_store(response)
        return response
    return await request_validation_exception_handler(request, exc)


def install_exception_handlers(app) -> None:
    """在 composition root 注册仅影响新世界端点的稳定错误信封。"""
    app.add_exception_handler(CompanionWorldApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
