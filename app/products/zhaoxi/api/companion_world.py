"""Companion World P1 控制面与 M3 文字 Feed owner API。"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.db import (
    SessionPrincipal,
    get_duplicate_reply_record,
    get_platform_user,
    try_conversation_turn_lock,
)
from app.products.zhaoxi.api.contracts import (
    WORLD_ERROR_RESPONSES,
    BootstrapResponse,
    CandidateListResponse,
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationReadResponse,
    FeedListResponse,
    FeedPostResponse,
    FeedRetireResponse,
    ResidentDraftPreviewResponse,
    ResidentListResponse,
    TurnResponse,
)
from app.platform.media.access import owner_scope, owner_ttl_seconds
from app.platform.media.assets import (
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VOICE,
    MediaRefInvalidError,
    read_media_file,
)
from app.platform.media.persistence import (
    MEDIA_STATUS_PENDING,
    MEDIA_STATUS_REFERENCED,
    get_media_asset,
    list_media_assets_unscoped,
)
from app.platform.media.view import (
    CONTENT_TYPE_IMAGE,
    build_feed_image_item,
    build_media_content,
    build_stored_content,
    decode_stored_content,
    media_preview_text,
    text_content,
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
    MAX_FEED_POST_IMAGES,
    CandidateRecord,
    CompanionWorldError,
    CompanionWorldFeedService,
    CompanionWorldService,
    ResidentRecord,
    ResidentSelection,
)
from app.products.zhaoxi.domain.companion_world.naming import naming_status
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
from app.products.zhaoxi.domain.missions.registry import mission_display_for_persona
from app.products.zhaoxi.application import (
    SqlCompanionWorldRepository,
    run_companion_world_turn,
)
from app.products.zhaoxi.application.companion_world_wish import (
    MAX_WISH_TEXT_CHARS,
    WishGenerationFailed,
    WishTextRejected,
    generate_wish_persona,
)
from app.schemas import MediaPayload
from app.time_utils import beijing_now
from app.routers.deps import _resolve_legacy_session_principal
from app.products.zhaoxi.manifest import (
    CANONICAL_API_PREFIX,
    PROXY_STRIPPED_API_PREFIX,
)

logger = logging.getLogger("ai4all.products.zhaoxi.companion_world")

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
    # 不存在、跨 owner、作者类型不符与格式非法统一收敛到这一个码，不给资源枚举信号。
    "post_not_found": 404,
    "post_not_hideable": 409,
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
    # v1.5 媒体（MEDIA-COMPAT-001，plan §6）。media_access_denied 刻意收敛所有失败原因
    # （签名不符 / 过期 / scope 已失效 / 行或文件缺失），不给资源枚举信号。
    "media_disabled": 404,
    "media_ref_invalid": 409,
    "media_ref_expired": 409,
    "media_kind_unsupported": 415,
    "media_decode_failed": 422,
    "media_too_large": 413,
    "media_duration_exceeded": 413,
    "media_count_exceeded": 422,
    "media_content_required": 422,
    "media_access_denied": 403,
    # 部署错误（MEDIA_URL_SIGNING_SECRET 未配置）：可重试，不是客户端的问题。
    "media_signing_unavailable": 503,
    # v1.5 许愿创建（WISH-001，plan §6）。清洗器不可用仍复用既有的
    # content_review_unavailable——同一个子系统同一个码；wish_generation_failed 专指
    # 「翻译成受控取值」那一步的模型不可用。两者对客户端都是"稍后重试"。
    "wish_text_rejected": 422,
    "wish_rate_limited": 429,
    "wish_generation_failed": 503,
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
    "/media/",
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
    """自建角色第一步。两条**互斥**路径，出参形状完全一致（客户端预览卡片零改动）：

    - **表单**（M1 起的既有路径）：结构化设定 + 可选自由文本，服务端清洗后渲染人设；
    - **许愿**（v1.5 WISH-001）：只给 ``wish_text`` + ``client_request_id``，服务端先清洗，
      再由 LLM 翻译成同一套受控取值，交给同一个渲染函数。

    结构化字段在此声明为可选**只是**为了让两条路径共用一个模型；表单路径的必填性由
    校验器保证，缺字段仍然是 422，与 v1.5 之前逐字节相同。
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=MAX_DISPLAY_NAME_CHARS)
    avatar_key: Optional[str] = Field(default=None, max_length=64)
    relationship_type: Optional[str] = Field(default=None, max_length=32)
    relationship_label: Optional[str] = Field(
        default=None, max_length=MAX_RELATIONSHIP_LABEL_CHARS
    )
    personality_traits: Optional[list[str]] = Field(
        default=None, max_length=MAX_PERSONALITY_TRAITS
    )
    style_note: Optional[str] = Field(default=None, max_length=MAX_STYLE_NOTE_CHARS)
    # 许愿路径。``client_request_id`` 是 preview 阶段的幂等键（WISH-005）：重试不产生
    # 第二份草稿、也不产生第二次 LLM 计费。它只对许愿路径有意义。
    wish_text: Optional[str] = Field(default=None, max_length=MAX_WISH_TEXT_CHARS)
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _clean_draft(self) -> "ResidentDraftPreviewPayload":
        self.name = (self.name or "").strip() or None
        self.avatar_key = (self.avatar_key or "").strip() or None
        self.relationship_type = (self.relationship_type or "").strip() or None
        self.relationship_label = (self.relationship_label or "").strip() or None
        self.style_note = (self.style_note or "").strip() or None
        self.wish_text = (self.wish_text or "").strip() or None
        self.client_request_id = (self.client_request_id or "").strip() or None

        has_structured = any(
            value is not None
            for value in (
                self.name,
                self.avatar_key,
                self.relationship_type,
                self.relationship_label,
                self.personality_traits,
                self.style_note,
            )
        )
        if self.wish_text:
            if has_structured:
                raise ValueError("wish_text is exclusive with structured fields")
            if not self.client_request_id:
                raise ValueError("wish_text requires client_request_id")
            if not _FEED_CLIENT_REQUEST_ID_RE.fullmatch(self.client_request_id):
                raise ValueError("invalid client_request_id")
            return self

        if self.client_request_id:
            raise ValueError("client_request_id only applies to wish_text")
        if not self.name:
            raise ValueError("name is required")
        if not self.avatar_key:
            raise ValueError("avatar_key is required")
        if not self.relationship_type:
            raise ValueError("relationship_type is required")
        traits = self.personality_traits or []
        if not (MIN_PERSONALITY_TRAITS <= len(traits) <= MAX_PERSONALITY_TRAITS):
            raise ValueError("personality_traits count is invalid")
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
    # v1.5：``text`` 与 ``media_ref`` 至少给一个（图片不带 caption 是常态）。
    # 「两个都空」刻意不在这里拦，而是由端点抛 ``media_content_required``——校验错误统一
    # 收敛成 ``invalid_request``，客户端分不出是格式错还是内容缺失。
    text: str = Field(default="", max_length=4000)
    media_ref: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _clean_turn(self) -> "ConversationTurnPayload":
        self.text = self.text.strip()
        self.media_ref = (self.media_ref or "").strip() or None
        if not _CLIENT_MESSAGE_ID_RE.fullmatch(self.client_message_id):
            raise ValueError("invalid turn payload")
        return self


class ConversationReadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 客户端把「已读到的最后一条」消息 id 报上来；服务端只前进不回退，也不会越过真实最新一条。
    last_message_id: int = Field(ge=1)


class EmptyFeedPayload(BaseModel):
    """隐藏动态不接受任何请求体字段：原因、作者、居民状态与世界 ID 全部由服务端决定。"""

    model_config = ConfigDict(extra="forbid")


class FeedPostPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=8, max_length=128)
    # v1.5：``text`` 默认空串以支持"只发图"；不带 media_refs 时仍要求非空正文——
    # 那是 v1.5 之前的载荷形状，它的 422 已是冻结行为，不能因为加了图片能力就变。
    text: str = ""
    media_refs: List[str] = Field(default_factory=list, max_length=MAX_FEED_POST_IMAGES)

    @model_validator(mode="after")
    def _clean_feed_post(self) -> "FeedPostPayload":
        self.client_request_id = self.client_request_id.strip()
        self.text = self.text.strip()
        self.media_refs = [str(ref or "").strip() for ref in self.media_refs]
        if not _FEED_CLIENT_REQUEST_ID_RE.fullmatch(self.client_request_id):
            raise ValueError("invalid client_request_id")
        if any(not ref or len(ref) > 64 for ref in self.media_refs):
            raise ValueError("invalid media_ref")
        if len(self.text) > 2000:
            raise ValueError("invalid feed text")
        if not self.text and not self.media_refs:
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
    """序列化候选公开字段；刻意不含 persona_seed_json 和内部 resident id。

    CAND-001/NAME-001 新增：``suggested_display_name`` 是服务端已快照的实例名（同一候选
    永远返回同值），``naming_status=unavailable`` 表示运营未配名池，客户端按契约回落到
    自己的本地兜底名池；``persona_key`` 供客户端跨模板版本认出同一个人设。
    """
    return {
        "template_id": candidate.template.id,
        "template_version": candidate.template_version,
        "name": candidate.template.name,
        "avatar_ref": candidate.template.avatar_ref,
        "summary": candidate.template.summary,
        "long_summary": candidate.template.long_summary,
        "tags": list(candidate.template.tags),
        "origin": candidate.origin,
        "status": candidate.status,
        "persona_key": candidate.template.persona_key,
        "suggested_display_name": candidate.suggested_display_name,
        "naming_version": candidate.naming_version,
        "naming_status": naming_status(candidate.suggested_display_name),
    }


def _resident_data(resident: ResidentRecord) -> dict:
    """序列化 owner 可见居民字段；不暴露 runtime account id。

    CONTENT-004：下发 ``mission_display`` 而不是 ``persona_key``——客户端要的是"这个居民的
    使命该不该显示计数"，给它人设 key 就等于把角色名单和分支规则又推回端上。
    """
    return {
        "resident_id": resident.resident_id,
        "name": resident.name,
        "avatar_ref": resident.avatar_ref,
        "status": resident.status,
        "origin": resident.origin,
        "conversation_id": resident.conversation_id,
        "conversation_state": resident.conversation_state,
        "mission_display": mission_display_for_persona(resident.persona_key),
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
        # 排序键与最近消息时间分开：没聊过的居民 last_message_at 为 null，但仍有稳定 sort_time。
        "last_message_at": _feed_time(item.last_message_at),
        "sort_time": _feed_time(item.sort_time),
        "can_send": item.can_send,
        "read_only_reason": item.read_only_reason,
    }


def chat_media_enabled(kind: str) -> bool:
    """聊天媒体按 kind 分别门控；与上传门控（图文动态也能开图片上传）刻意分开。"""
    if kind == MEDIA_KIND_IMAGE:
        return bool(getattr(settings, "companion_world_chat_image_enabled", False))
    if kind == MEDIA_KIND_VOICE:
        return bool(getattr(settings, "companion_world_chat_voice_enabled", False))
    return False


def resolve_chat_media(*, media_ref: str, platform_user_id: str) -> Dict[str, Any]:
    """把 ``media_ref`` 解析成一条可发送的资产行（AI 会话与真人会话共用）。

    只判「能不能发」：owner 锚定（跨 owner 与不存在合并成同一码，不给资源枚举信号）、
    kind 门控、状态与回收期。真正的认领（``pending`` → ``referenced``）必须留到消息落库的
    **同一事务**里做，在这里翻状态会在后续失败时留下永不回收的孤儿。

    ``referenced`` 也放行，一次性语义由那次原子认领独家把守：客户端拿同一个
    ``client_message_id`` 重试时资产已经是 ``referenced``，在这里拒掉会让本该幂等的重试
    报错；而真的拿旧资产发新消息时，认领会失败并收敛成同一个 ``media_ref_invalid``。
    """
    asset = get_media_asset(
        media_id=media_ref, owner_platform_user_id=platform_user_id
    )
    if asset is None:
        raise CompanionWorldApiError("media_ref_invalid")
    if not chat_media_enabled(str(asset.get("kind") or "")):
        raise CompanionWorldApiError("media_disabled")
    if str(asset.get("status") or "") not in {
        MEDIA_STATUS_PENDING,
        MEDIA_STATUS_REFERENCED,
    }:
        raise CompanionWorldApiError("media_ref_invalid")
    expires_at = str(asset.get("expires_at") or "")
    if expires_at and expires_at <= beijing_now().strftime("%Y-%m-%d %H:%M:%S"):
        # 回收 job 还没扫到但已过期；提前拒绝，避免发出去转头文件就没了。
        raise CompanionWorldApiError("media_ref_expired")
    return asset


def feed_image_enabled() -> bool:
    """图文动态门控；与聊天图片分开，运营可以只放开一侧。"""
    return bool(getattr(settings, "companion_world_feed_image_enabled", False))


def resolve_feed_media(*, media_ref: str, platform_user_id: str) -> Dict[str, Any]:
    """把动态里的 ``media_ref`` 解析成一条可发布的图片资产行。

    与 :func:`resolve_chat_media` 同构（owner 锚定、状态与回收期、``referenced`` 放行留给
    发布事务里的原子认领独家把守），两点差别：门控走 feed 开关，且**只收图片**——动态不支持
    语音，把语音资产挂上去在这里就拒（合并成同一个 ``media_ref_invalid``，不给 kind 探测信号）。
    """
    asset = get_media_asset(
        media_id=media_ref, owner_platform_user_id=platform_user_id
    )
    if asset is None:
        raise CompanionWorldApiError("media_ref_invalid")
    if not feed_image_enabled():
        raise CompanionWorldApiError("media_disabled")
    if str(asset.get("kind") or "") != MEDIA_KIND_IMAGE:
        raise CompanionWorldApiError("media_ref_invalid")
    if str(asset.get("status") or "") not in {
        MEDIA_STATUS_PENDING,
        MEDIA_STATUS_REFERENCED,
    }:
        raise CompanionWorldApiError("media_ref_invalid")
    expires_at = str(asset.get("expires_at") or "")
    if expires_at and expires_at <= beijing_now().strftime("%Y-%m-%d %H:%M:%S"):
        raise CompanionWorldApiError("media_ref_expired")
    return asset


def build_feed_content(
    *,
    text: Optional[str],
    media_ids: Sequence[str],
    assets: Dict[str, Dict[str, Any]],
    scope: str,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """按 D-1 投影一条动态的 ``content``；主人与访客两条读路径共用。

    ``scope``/``ttl_seconds`` 由调用方按"看的人是谁"给定（主人 ``pu:``、访客 ``visit:``），
    本函数不猜。资产行全丢失（回收竞态/人工删除）时降级成纯文本动态而不是空 ``images``，
    免得客户端渲染出一个没有任何内容的图文卡片。
    """
    items: List[Dict[str, Any]] = []
    for media_id in media_ids:
        asset = assets.get(str(media_id))
        if asset is None:
            continue
        item = build_feed_image_item(
            asset=asset, scope=scope, ttl_seconds=ttl_seconds
        )
        if item is not None:
            items.append(item)
    if not items:
        return text_content(text)
    return {"type": CONTENT_TYPE_IMAGE, "text": str(text or ""), "images": items}


def _ai_message_data(
    item,
    *,
    assets: Dict[str, Dict[str, Any]],
    scope: str,
    ttl_seconds: int,
) -> dict:
    """把一条 AI 会话消息投影成 D-1 的判别联合。

    ``content`` 是唯一权威来源；``message_type``/``text`` 是兼容老客户端的镜像字段，恒与
    ``content`` 一致。资产行缺失（回收竞态/人工删除）时降级成纯文本，不 500。
    """
    stored = decode_stored_content(item.content_json)
    if not item.media_id:
        # 存量消息与非媒体消息：库里的 content 就是用户原文。
        content = text_content(stored["text"] if stored else item.content)
    else:
        # 媒体消息的正文只能取 content_json 的 caption——item.content 含 VL 描述（D-2 红线）。
        caption = stored["text"] if stored else ""
        asset = assets.get(str(item.media_id))
        content = (
            build_media_content(
                asset=asset, caption=caption, scope=scope, ttl_seconds=ttl_seconds
            )
            if asset is not None
            else text_content(caption)
        )
    return {
        "id": item.id,
        "message_id": item.message_id,
        "role": item.role,
        "message_type": content["type"],
        "text": content["text"],
        "content": content,
        "created_at": _feed_time(item.created_at),
    }


def _voice_llm_text(*, caption: str, transcript: str) -> str:
    """语音轮喂给模型的上下文文本：转写为主，caption 在前（D-5）。

    两者皆空时返回空串——Runtime 会据此走「没听清」兜底话术，不让主模型对着空内容瞎猜。
    """
    return "\n".join(part for part in (caption.strip(), transcript.strip()) if part)


def _turn_media_kwargs(
    *, asset: Optional[Dict[str, Any]], caption: str, platform_user_id: str
) -> Dict[str, Any]:
    """把一条待发送资产翻译成 ``run_companion_world_turn`` 的媒体入参（D-2 / D-5）。

    三份文本口径**刻意不同**：``text`` 是 LLM 上下文（图片轮的 VL 描述由 Runtime 合成，
    语音轮在这里拼上转写）；``display_content`` 只留用户自己写的 caption；资产元信息一律
    读时从 ``media_assets`` 现取，不冗余落库。
    """
    if asset is None:
        return {"text": caption, "message_type": "text"}
    kind = str(asset.get("kind") or "")
    kwargs: Dict[str, Any] = {
        "display_content": build_stored_content(kind=kind, caption=caption),
        "media_asset_id": str(asset.get("id") or ""),
        "media_asset_owner_id": platform_user_id,
    }
    if kind == MEDIA_KIND_IMAGE:
        kwargs["message_type"] = "image"
        kwargs["text"] = caption
        # 走内联字节而非 path：``describe_image`` 的本地路径分支限死在 image_inbound_dir
        # 白名单内，媒体库不在其中；字节路径无路径概念，仍受 image_max_bytes 保护。
        try:
            raw = read_media_file(str(asset.get("storage_path") or ""))
            kwargs["media"] = MediaPayload(
                media_id=str(asset.get("id") or ""),
                data_base64=base64.b64encode(raw).decode("ascii"),
                format=str(asset.get("mime") or "image/jpeg"),
                size=len(raw),
            )
        except (FileNotFoundError, ValueError):
            # 文件没了（人工干预或回收竞态）：消息照发，VL 拿不到字节自动落兜底话术。
            logger.warning("chat_media_file_missing media_id=%s", asset.get("id"))
            kwargs["media"] = None
    else:
        kwargs["message_type"] = "voice"
        kwargs["text"] = _voice_llm_text(
            caption=caption, transcript=str(asset.get("transcript") or "")
        )
    return kwargs


def _turn_data(
    reply_row: Optional[Dict[str, Any]],
    *,
    deduplicated: bool = True,
    no_reply: bool = False,
) -> dict:
    """冻结 turn 响应形状（TURN-001）。

    ``reply`` 要么是 ``{text, message_id}`` 且 ``text`` 非空，要么整体为 ``null``——不再出现
    「有 reply 对象但 text 为 null」这种要客户端二次判断的中间态。幂等重放回放**原持久化**的
    ``message_id``，客户端据此认出是同一条消息，不新建气泡。
    """
    if reply_row is None or not reply_row.get("content"):
        return {"reply": None, "no_reply": no_reply, "deduplicated": deduplicated}
    return {
        "reply": {
            "text": str(reply_row["content"]),
            "message_id": reply_row.get("message_id"),
        },
        "no_reply": no_reply,
        "deduplicated": deduplicated,
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


def _feed_item_data(
    item,
    *,
    assets: Optional[Dict[str, Dict[str, Any]]] = None,
    scope: Optional[str] = None,
    ttl_seconds: int = 0,
) -> dict:
    """序列化 Feed 公开 DTO，不泄漏 owner/runtime/fingerprint/outbox 字段。

    ``assets``/``scope`` 缺省时按纯文本投影：AI 动态与发布响应之外的路径不带图，不必为了
    形状统一去多查一次库。
    """
    content = (
        build_feed_content(
            text=item.text,
            media_ids=item.media_ids,
            assets=assets,
            scope=scope,
            ttl_seconds=ttl_seconds,
        )
        if item.media_ids and assets is not None and scope
        # 无图动态保持 v1 形状原样（``text`` 允许为 null，不改成空串）。
        else {"type": "text", "text": item.text}
    )
    return {
        "post_id": item.id,
        "author": {
            "type": item.author_type,
            "resident_id": item.author_resident_id,
            "name": item.author_name,
            "avatar_ref": item.author_avatar_ref,
        },
        "content": content,
        "post_type": item.post_type,
        "source": item.source_type,
        "published_at": _feed_time(item.published_at),
    }


def _feed_retire_data(item, *, replayed: bool) -> dict:
    """下架结果 DTO：库里终态统一是 ``deleted``，对外按原因投影成 deleted/hidden。"""
    return {
        "post_id": item.id,
        "status": "hidden" if item.terminal_reason == "owner_hidden" else "deleted",
        "replayed": replayed,
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


@router.post(
    "/worlds/home/bootstrap",
    response_model=BootstrapResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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


@router.get(
    "/worlds/home/resident-candidates",
    response_model=CandidateListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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


@router.post(
    "/worlds/home/residents/confirm",
    response_model=ResidentListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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


@router.get(
    "/worlds/home/residents",
    response_model=ResidentListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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


def _draft_preview_data(draft, rendered) -> dict:
    """预览回显。表单、许愿与幂等重放三条路径共用这一份形状（WISH-002）。"""
    return {
        "draft_id": draft.id,
        "draft_token": draft.draft_token,
        "expires_at": _feed_time(draft.expires_at),
        "name": draft.name,
        "avatar_ref": resolve_avatar_ref(draft.avatar_key),
        "relationship_display": rendered.relationship_display,
        "tags": list(rendered.tags),
        "normalized_summary": draft.normalized_summary,
        "ai_identity_notice": rendered.ai_identity_notice,
    }


def _wish_daily_max() -> int:
    """许愿日额度；<=0 视为不限制。"""
    try:
        return int(getattr(settings, "companion_world_wish_daily_max", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _preview_from_wish(
    payload: ResidentDraftPreviewPayload,
    *,
    platform_user_id: str,
    now: datetime,
) -> dict:
    """许愿路径：幂等重放 → 额度 → 清洗 → LLM 翻译成受控取值 → 落草稿。

    顺序刻意如此：重放与额度都在生成**之前**判，重试和超额都不会白花一次模型调用。
    """
    if not bool(getattr(settings, "companion_world_resident_wish_enabled", False)):
        raise CompanionWorldApiError("feature_disabled", 404)
    wish_request_id = payload.client_request_id or ""
    service = _service()
    replay = _run_domain(
        lambda: service.begin_wish_preview(
            platform_user_id,
            wish_request_id=wish_request_id,
            window_start=_db_time(now - timedelta(days=1)),
            daily_max=_wish_daily_max(),
        )
    )
    if replay is not None:
        return _draft_preview_data(*replay)

    try:
        persona, safety = generate_wish_persona(payload.wish_text or "")
    except WishTextRejected as err:
        logger.info("wish.rejected categories=%s", ",".join(err.categories))
        raise CompanionWorldApiError("wish_text_rejected") from err
    except TextSanitizerUnavailable as err:
        raise CompanionWorldApiError("content_review_unavailable") from err
    except WishGenerationFailed as err:
        raise CompanionWorldApiError("wish_generation_failed") from err

    try:
        draft, rendered = _run_domain(
            lambda: service.preview_resident_draft(
                platform_user_id,
                persona,
                draft_token=secrets.token_urlsafe(32),
                expires_at=_db_time(
                    now + timedelta(minutes=RESIDENT_DRAFT_TTL_MINUTES)
                ),
                safety_json=json.dumps(safety, ensure_ascii=False),
                source="wish",
                wish_request_id=wish_request_id,
            )
        )
    except CompanionWorldApiError:
        raise
    except Exception:
        # 同一 wish_request_id 并发预览：唯一索引挡下第二笔，回读先到的那份草稿即可，
        # 客户端两次请求拿到同一个 draft_token（而不是一个 500）。
        existing = _run_domain(
            lambda: service.begin_wish_preview(
                platform_user_id,
                wish_request_id=wish_request_id,
                window_start=_db_time(now - timedelta(days=1)),
                daily_max=0,
            )
        )
        if existing is None:
            raise
        return _draft_preview_data(*existing)
    return _draft_preview_data(draft, rendered)


@router.post(
    "/worlds/home/resident-drafts/preview",
    response_model=ResidentDraftPreviewResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def preview_resident_draft(
    payload: ResidentDraftPreviewPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    """自建角色第一步：清洗自由文本 → 渲染人设 → 返回可预览摘要与一次性 draft_token。

    清洗在**入口一次**完成，落库与后续渲染只用清洗结果；原文不落库（SEC-001 / D-B）。
    许愿路径（``wish_text``）在清洗之后多一步「LLM 翻译成受控取值」，此后与表单路径
    完全同源——自由文本仍然不直通人设。
    """
    now = beijing_now()
    if payload.wish_text:
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data=_preview_from_wish(
                payload, platform_user_id=principal.platform_user_id, now=now
            ),
        )

    safety: Dict[str, dict] = {}
    persona = PersonaInput(
        name=_sanitize_required(
            payload.name or "",
            field_kind=FIELD_DISPLAY_NAME,
            max_chars=MAX_DISPLAY_NAME_CHARS,
            safety=safety,
        ),
        avatar_key=payload.avatar_key or "",
        relationship_type=payload.relationship_type or "",
        relationship_label=_sanitize_optional(
            payload.relationship_label,
            field_kind=FIELD_RELATIONSHIP_LABEL,
            max_chars=MAX_RELATIONSHIP_LABEL_CHARS,
            safety=safety,
        ),
        personality_traits=tuple(payload.personality_traits or ()),
        style_note=_sanitize_optional(
            payload.style_note,
            field_kind=FIELD_STYLE_NOTE,
            max_chars=MAX_STYLE_NOTE_CHARS,
            safety=safety,
        ),
    )
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
    return _envelope(request, code="ok", data=_draft_preview_data(draft, rendered))


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


@router.get(
    "/worlds/home/feed",
    response_model=FeedListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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
    # 一次批量取本页所有图，避免逐条查库；主人自己的 Feed 恒用 owner scope 签 URL。
    assets = list_media_assets_unscoped(
        media_ids=[media_id for item in page for media_id in item.media_ids]
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "items": [
                _feed_item_data(
                    item,
                    assets=assets,
                    scope=owner_scope(principal.platform_user_id),
                    ttl_seconds=owner_ttl_seconds(),
                )
                for item in page
            ],
            "next_cursor": (
                _encode_feed_cursor(page[-1]) if len(rows) > limit and page else None
            ),
        },
    )


@router.post(
    "/worlds/home/feed/posts",
    response_model=FeedPostResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def publish_home_feed_post(
    payload: FeedPostPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_feed_session),
) -> dict:
    # 先逐个解析 media_ref（门控/归属/回收期），再进发布事务；真正的认领在事务内完成。
    assets = {
        str(asset["id"]): asset
        for asset in (
            resolve_feed_media(
                media_ref=media_ref, platform_user_id=principal.platform_user_id
            )
            for media_ref in payload.media_refs
        )
    }
    post, created = _run_domain(
        lambda: _feed_service().publish_user_post(
            principal.platform_user_id,
            client_request_id=payload.client_request_id,
            text=payload.text,
            published_at=beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
            media_ids=payload.media_refs,
        )
    )
    response.status_code = 201 if created else 200
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "post": _feed_item_data(
                post,
                assets=assets,
                scope=owner_scope(principal.platform_user_id),
                ttl_seconds=owner_ttl_seconds(),
            )
        },
    )


@router.delete(
    "/worlds/home/feed/posts/{post_id}",
    response_model=FeedRetireResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def delete_home_feed_post(
    post_id: str,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_feed_session),
) -> dict:
    """主人删除自己发布的文字动态（FEED-MGMT-001）。

    只作用于当前 Session 主人的 home world；AI 居民动态走隐藏语义，这里一律按
    ``post_not_found`` 拒绝。删除后主人与所有有效访客再次读 Feed 立即不可见。
    重复删除幂等，回放首次结果并置 ``replayed=true``，不产生 5xx。
    """
    post, changed = _run_domain(
        lambda: _feed_service().retire_post(
            principal.platform_user_id,
            post_id=post_id,
            mode="delete",
            retired_at=beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data=_feed_retire_data(post, replayed=not changed),
    )


@router.post(
    "/worlds/home/feed/posts/{post_id}/hide",
    response_model=FeedRetireResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def hide_home_feed_post(
    post_id: str,
    request: Request,
    response: Response,
    payload: Optional[EmptyFeedPayload] = None,
    principal: SessionPrincipal = Depends(_require_feed_session),
) -> dict:
    """主人隐藏本世界内 AI 居民发布的动态（FEED-MGMT-002）。

    隐藏是服务端权威状态，不是单设备偏好：主人与所有有效访客后续都读不到。不接受客户端
    提供原因、作者或世界 ID；不修改居民生命周期、会话、记忆或离开状态。离别动态
    （``post_type='farewell'``）M2 不允许隐藏，返回 ``post_not_hideable``。
    """
    post, changed = _run_domain(
        lambda: _feed_service().retire_post(
            principal.platform_user_id,
            post_id=post_id,
            mode="hide",
            retired_at=beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data=_feed_retire_data(post, replayed=not changed),
    )


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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


@router.get(
    "/ai-conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
    responses=WORLD_ERROR_RESPONSES,
)
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
    # 媒体资产一次批量取回（避免逐条 N+1）；这一页的消息已经证明属于本人，因此可用
    # unscoped 批读——AI 会话里的媒体恒由 owner 自己上传，owner 锚在上面的查询已经加过。
    assets = list_media_assets_unscoped(
        media_ids=[item.media_id for item in messages if item.media_id]
    )
    scope = owner_scope(principal.platform_user_id)
    ttl = owner_ttl_seconds()
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "state": target.state,
            "messages": [
                _ai_message_data(item, assets=assets, scope=scope, ttl_seconds=ttl)
                for item in messages
            ],
            "next_cursor": messages[0].id if len(messages) == limit else None,
        },
    )


@router.post(
    "/ai-conversations/{conversation_id}/read",
    response_model=ConversationReadResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def mark_conversation_read(
    conversation_id: str,
    payload: ConversationReadPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    """把会话标记为已读到某条消息（CONV-002）。幂等：重放同一 id 结果不变。"""
    state = _run_domain(
        lambda: _service().mark_conversation_read(
            principal.platform_user_id,
            conversation_id,
            last_message_id=payload.last_message_id,
        )
    )
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={
            "conversation_id": conversation_id,
            "last_read_message_id": state.last_read_message_id,
            "unread": state.unread,
        },
    )


@router.post(
    "/ai-conversations/{conversation_id}/turn",
    response_model=TurnResponse,
    responses=WORLD_ERROR_RESPONSES,
)
def conversation_turn(
    conversation_id: str,
    payload: ConversationTurnPayload,
    request: Request,
    response: Response,
    principal: SessionPrincipal = Depends(_require_world_session),
) -> dict:
    service = _service()
    if payload.media_ref:
        media_asset = resolve_chat_media(
            media_ref=payload.media_ref, platform_user_id=principal.platform_user_id
        )
    elif not payload.text:
        raise CompanionWorldApiError("media_content_required")
    else:
        media_asset = None
    target = _run_domain(
        lambda: service.resolve_conversation(
            principal.platform_user_id, conversation_id
        )
    )
    # 用客户端已知的 conversation 锚定幂等键，避免内部 runtime account 出现在历史响应。
    mapped_message_id = f"app:{target.conversation_id}:{payload.client_message_id}"
    duplicate = get_duplicate_reply_record(
        account_id=target.runtime_account_id,
        reply_to_message_id=mapped_message_id,
    )
    if duplicate is not None:
        _no_store(response)
        return _envelope(request, code="ok", data=_turn_data(duplicate))

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
        duplicate = get_duplicate_reply_record(
            account_id=target.runtime_account_id,
            reply_to_message_id=mapped_message_id,
        )
        if duplicate is not None:
            result = None
        else:
            platform_user = get_platform_user(
                platform_user_id=principal.platform_user_id
            ) or {}
            try:
                result = run_companion_world_turn(
                    conversation_id=target.conversation_id,
                    universe_id=target.universe_id,
                    resident_id=target.resident_id,
                    runtime_account_id=target.runtime_account_id,
                    platform_user_id=principal.platform_user_id,
                    sender_name=platform_user.get("display_name"),
                    message_id=mapped_message_id,
                    **_turn_media_kwargs(
                        asset=media_asset,
                        caption=payload.text,
                        platform_user_id=principal.platform_user_id,
                    ),
                )
            except MediaRefInvalidError as err:
                # 资产在校验之后、认领之前被并发抢走或回收；入站消息已随事务一起回滚。
                raise CompanionWorldApiError("media_ref_invalid") from err

    if result is None:
        data = _turn_data(duplicate)
    else:
        if result.status == "rate_limited":
            raise CompanionWorldApiError("rate_limited")
        if result.status == "disabled":
            raise CompanionWorldApiError("account_disabled")
        data = _turn_data(
            None
            if result.no_reply
            else {
                "content": result.reply,
                "message_id": result.metadata.get("reply_message_id"),
            },
            deduplicated=result.status == "duplicate",
            no_reply=result.no_reply,
        )
    _no_store(response)
    return _envelope(request, code="ok", data=data)


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
