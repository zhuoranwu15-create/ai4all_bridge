"""Companion World P1 控制面与 M3 文字 Feed owner API。"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

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
from app.platform.moderation.text_sanitizer import (
    FIELD_DISPLAY_NAME,
    FIELD_RELATIONSHIP_LABEL,
    FIELD_STYLE_NOTE,
    TextRejected,
    TextSanitizerUnavailable,
    sanitize_text,
)
from app.products.zhaoxi.domain.companion_world import (
    CandidateRecord,
    CompanionWorldError,
    CompanionWorldFeedService,
    CompanionWorldService,
    ResidentRecord,
    ResidentSelection,
)
from app.products.zhaoxi.domain.companion_world.persona_catalog import (
    MAX_DISPLAY_NAME_CHARS,
    MAX_PERSONALITY_TRAITS,
    MAX_RELATIONSHIP_LABEL_CHARS,
    MAX_STYLE_NOTE_CHARS,
    MIN_PERSONALITY_TRAITS,
    PersonaInput,
    options_catalog,
    resolve_avatar_ref,
)
from app.products.zhaoxi.application import (
    SqlCompanionWorldRepository,
    run_companion_world_turn,
)
from app.time_utils import beijing_now
from app.routers.deps import _resolve_legacy_session_principal
from app.products.zhaoxi.manifest import (
    CANONICAL_API_PREFIX,
    PROXY_STRIPPED_API_PREFIX,
)

router = APIRouter(tags=["companion-world"])

_ERROR_STATUS = {
    "not_found": 404,
    # 能力被 flag 关闭。刻意与 not_found 分开：客户端据此区分「这个功能没开」和
    # 「这个资源不存在」，配合 /app/config 的 capability 决定要不要发这个请求。
    "feature_disabled": 404,
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
    # 自建角色受控取值与草稿生命周期
    "avatar_key_invalid": 400,
    "relationship_type_invalid": 400,
    "relationship_label_required": 400,
    "personality_trait_invalid": 400,
    "personality_trait_count_invalid": 400,
    "display_name_invalid": 400,
    "resident_draft_not_found": 404,
    "resident_draft_expired": 409,
    "resident_draft_consumed": 409,
    # D-B：自由文本清洗器 fail closed（可重试）与硬拒绝红线（不可重试）
    "content_review_unavailable": 503,
    "content_rejected": 422,
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
# 自建角色草稿有效期。短期 + 单次消费：预览页停留过久应重新预览，避免陈旧人设被下单。
RESIDENT_DRAFT_TTL_MINUTES = 30

# 世界端点在剥掉挂载前缀后的路径。用于给 422 套上统一错误信封。
_COMPANION_WORLD_ROUTES = (
    "/worlds/",
    "/conversations",
    "/ai-conversations/",
    "/notifications",
    "/world/invites",
    "/visits",
    "/human-conversations",
    "/mailbox/",
)
# 长前缀优先，否则 "/v1" 会先吃掉 "/v1/products/zhaoxi"。
_MOUNT_PREFIXES = tuple(
    sorted(
        ("/v1", CANONICAL_API_PREFIX, PROXY_STRIPPED_API_PREFIX),
        key=len,
        reverse=True,
    )
)


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


class ResidentDraftPreviewPayload(BaseModel):
    """自建角色第一步：结构化设定 + 自由文本，服务端清洗后渲染出可预览的人设。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_CHARS)
    avatar_key: str = Field(min_length=1, max_length=64)
    relationship_type: str = Field(min_length=1, max_length=32)
    relationship_label: Optional[str] = Field(
        default=None, max_length=MAX_RELATIONSHIP_LABEL_CHARS
    )
    personality_traits: list[str] = Field(
        min_length=MIN_PERSONALITY_TRAITS, max_length=MAX_PERSONALITY_TRAITS
    )
    style_note: Optional[str] = Field(default=None, max_length=MAX_STYLE_NOTE_CHARS)

    @model_validator(mode="after")
    def _clean_draft(self) -> "ResidentDraftPreviewPayload":
        self.name = self.name.strip()
        self.avatar_key = self.avatar_key.strip()
        self.relationship_type = self.relationship_type.strip()
        self.relationship_label = (self.relationship_label or "").strip() or None
        self.style_note = (self.style_note or "").strip() or None
        if not self.name:
            raise ValueError("name is required")
        return self


class CreateResidentPayload(BaseModel):
    """新增居民。两条路径互斥：预设 `template_id`，或自建 `draft_token + client_request_id`。

    M1 已下线裸 `name + persona_hint` 路径 —— 自由文本不再直通人设（SEC-001 / D-B）。
    """

    model_config = ConfigDict(extra="forbid")

    template_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    draft_token: Optional[str] = Field(default=None, min_length=16, max_length=128)
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CreateResidentPayload":
        self.template_id = (self.template_id or "").strip() or None
        self.draft_token = (self.draft_token or "").strip() or None
        self.client_request_id = (self.client_request_id or "").strip() or None
        if bool(self.template_id) == bool(self.draft_token):
            raise ValueError("provide exactly one of template_id or draft_token")
        if self.draft_token:
            if not self.client_request_id:
                raise ValueError("draft_token requires client_request_id")
            if not _FEED_CLIENT_REQUEST_ID_RE.fullmatch(self.client_request_id):
                raise ValueError("invalid client_request_id")
        elif self.client_request_id:
            raise ValueError("client_request_id only applies to draft_token")
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
        raise CompanionWorldApiError("feature_disabled", 404)
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
        raise CompanionWorldApiError("feature_disabled", 404)
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


def _db_time(value: datetime) -> str:
    """把北京时间转成 DB 存储用的 naive 串，与各表 DEFAULT 的格式一致。"""
    return value.strftime("%Y-%m-%d %H:%M:%S")


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
            # 已经在世界里的居民（微信带入的 legacy 角色即在此）。selecting 阶段客户端
            # 要把它们和候选一起展示，但它们不可被叉掉。
            "existing_residents": [_resident_data(item) for item in result.residents],
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


@router.get("/worlds/home/resident-options")
def resident_options(
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    """自建角色的受控取值表。客户端据此渲染选择器，**不得硬编码枚举**。"""
    _no_store(response)
    return _envelope(request, code="ok", data=options_catalog())


def _sanitize_optional(
    text: Optional[str],
    *,
    field_kind: str,
    max_chars: int,
    safety: Dict[str, dict],
) -> Optional[str]:
    """可选自由文本清洗；空值直接跳过，不浪费一次 LLM 调用。"""
    if not text:
        return None
    return (
        _sanitize_required(
            text, field_kind=field_kind, max_chars=max_chars, safety=safety
        )
        or None
    )


def _sanitize_required(
    text: str, *, field_kind: str, max_chars: int, safety: Dict[str, dict]
) -> str:
    """必填自由文本清洗；改写结果直接作为最终值返回（Q7：不提示「内容已被修改」）。

    判定结果按字段写入 ``safety`` 留痕（只存 verdict 与风险分类，不存原文）。
    """
    try:
        result = sanitize_text(text=text, field_kind=field_kind, max_chars=max_chars)
    except TextRejected as err:
        raise CompanionWorldApiError("content_rejected") from err
    except TextSanitizerUnavailable as err:
        raise CompanionWorldApiError("content_review_unavailable") from err
    safety[field_kind] = result.as_safety_record()
    return result.text


@router.post("/worlds/home/resident-drafts/preview")
def preview_resident_draft(
    payload: ResidentDraftPreviewPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    """自建角色第一步：清洗自由文本 → 渲染人设 → 返回可预览摘要与一次性 draft_token。

    清洗在**入口一次**完成，落库与后续渲染只用清洗结果；原文不落库（SEC-001 / D-B）。
    """
    safety: Dict[str, dict] = {}
    persona = PersonaInput(
        name=_sanitize_required(
            payload.name,
            field_kind=FIELD_DISPLAY_NAME,
            max_chars=MAX_DISPLAY_NAME_CHARS,
            safety=safety,
        ),
        avatar_key=payload.avatar_key,
        relationship_type=payload.relationship_type,
        relationship_label=_sanitize_optional(
            payload.relationship_label,
            field_kind=FIELD_RELATIONSHIP_LABEL,
            max_chars=MAX_RELATIONSHIP_LABEL_CHARS,
            safety=safety,
        ),
        personality_traits=tuple(payload.personality_traits),
        style_note=_sanitize_optional(
            payload.style_note,
            field_kind=FIELD_STYLE_NOTE,
            max_chars=MAX_STYLE_NOTE_CHARS,
            safety=safety,
        ),
    )
    now = beijing_now()
    draft, rendered = _run_domain(
        lambda: _service().preview_resident_draft(
            principal.platform_user_id,
            persona,
            draft_token=secrets.token_urlsafe(32),
            expires_at=_db_time(now + timedelta(minutes=RESIDENT_DRAFT_TTL_MINUTES)),
            safety_json=json.dumps(safety, ensure_ascii=False),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "draft_id": draft.id,
            "draft_token": draft.draft_token,
            "expires_at": _feed_time(draft.expires_at),
            "name": draft.name,
            "avatar_ref": resolve_avatar_ref(draft.avatar_key),
            "relationship_display": rendered.relationship_display,
            "tags": list(rendered.tags),
            "normalized_summary": draft.normalized_summary,
            "ai_identity_notice": rendered.ai_identity_notice,
        },
    )


@router.post("/worlds/home/residents")
def create_resident(
    payload: CreateResidentPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    if payload.draft_token:
        result = _run_domain(
            lambda: _service().create_resident_from_draft(
                principal.platform_user_id,
                draft_token=payload.draft_token or "",
                client_request_id=payload.client_request_id or "",
                now=_db_time(beijing_now()),
            )
        )
    else:
        result = _run_domain(
            lambda: _service().create_resident(
                principal.platform_user_id, template_id=payload.template_id
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


def _is_companion_world_path(path: str) -> bool:
    """判断请求是否落在世界端点上，三条挂载前缀一视同仁。

    产品 router 同时挂在 legacy `/v1`、规范命名空间和反代剥前缀三条路径上（见 manifest）。
    以前这里只匹配 `/v1/...`，导致我们**推荐**客户端使用的规范前缀反而拿不到统一错误信封，
    422 会退化成 FastAPI 默认的 `{"detail": [...]}`。这里先剥掉挂载前缀再判断。
    """
    for prefix in _MOUNT_PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix):].startswith(_COMPANION_WORLD_ROUTES)
    return False


async def _api_error_handler(request: Request, exc: CompanionWorldApiError):
    response = JSONResponse(
        status_code=exc.status_code,
        content=_envelope(request, code=exc.code),
    )
    _no_store(response)
    return response


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    if _is_companion_world_path(request.url.path):
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
