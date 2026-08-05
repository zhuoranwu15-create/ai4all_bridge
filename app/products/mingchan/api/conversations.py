"""鸣蝉居民会话 HTTP API；当前写入面只接受纯文本。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from app.bootstrap.product_registry import (
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import SessionPrincipal, get_duplicate_reply_record, get_platform_user
from app.products.mingchan.api.contracts import (
    MingchanConversationListResponse,
    MingchanConversationMessagesResponse,
    MingchanConversationReadRequest,
    MingchanConversationReadResponse,
    MingchanConversationTurnRequest,
    MingchanConversationTurnResponse,
)
from app.products.mingchan.api.deps import (
    build_mingchan_enabled_dependency,
    build_mingchan_session_dependency,
)
from app.products.mingchan.api.world_onboarding import (
    MingchanWorldApiError,
    _envelope,
    _no_store,
)
from app.products.mingchan.api.world import (
    _ai_message_data,
    _turn_media_kwargs,
    resolve_chat_media,
)
from app.products.mingchan.api.world_contracts import WORLD_ERROR_RESPONSES
from app.products.mingchan.application.companion_world_turn import (
    run_mingchan_companion_world_turn,
)
from app.products.mingchan.domain.companion_world.conversations import (
    ConversationMessage,
    MingchanConversationError,
    MingchanConversationService,
)
from app.products.mingchan.infrastructure.conversations import (
    SqlMingchanConversationRepository,
)
from app.products.mingchan.infrastructure.persistence.companion_world import (
    try_conversation_turn_lock,
)
from app.platform.media.access import owner_scope, owner_ttl_seconds
from app.platform.media.assets import MediaRefInvalidError
from app.platform.media.persistence import list_media_assets_unscoped

_BEIJING_TZ = timezone(timedelta(hours=8))


def _run_domain(action: Callable):
    try:
        return action()
    except MingchanConversationError as err:
        raise MingchanWorldApiError(err.code) from err


def _public_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BEIJING_TZ)
    return parsed.isoformat()


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
        "last_message_at": _public_time(item.last_message_at),
        "sort_time": _public_time(item.sort_time),
        "can_send": item.can_send,
        "read_only_reason": item.read_only_reason,
    }


def _turn_data(
    reply_row: Optional[dict],
    *,
    deduplicated: bool = True,
    no_reply: bool = False,
) -> dict:
    if reply_row is None or not reply_row.get("content"):
        return {
            "reply": None,
            "no_reply": no_reply,
            "deduplicated": deduplicated,
        }
    return {
        "reply": {
            "text": str(reply_row["content"]),
            "message_id": reply_row.get("message_id"),
        },
        "no_reply": no_reply,
        "deduplicated": deduplicated,
    }


def build_router(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    *,
    config=None,
) -> APIRouter:
    """创建固定鸣蝉 audience 的居民会话 router。"""

    world_config = settings if config is None else config
    require_enabled = build_mingchan_enabled_dependency(registry)
    require_session = build_mingchan_session_dependency(registry)

    def require_conversation_session(
        authorization: Optional[str] = Header(default=None),
    ) -> SessionPrincipal:
        if not bool(getattr(world_config, "mingchan_p1_enabled", False)):
            raise MingchanWorldApiError("feature_disabled")
        try:
            return require_session(authorization=authorization)
        except HTTPException as err:
            raise MingchanWorldApiError("unauthorized", 401) from err

    def service() -> MingchanConversationService:
        return MingchanConversationService(
            SqlMingchanConversationRepository(registry=registry)
        )

    router = APIRouter(
        tags=["mingchan-conversations"],
        dependencies=[Depends(require_enabled)],
    )

    @router.get(
        "/conversations",
        response_model=MingchanConversationListResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def list_conversations(
        request: Request,
        response: Response,
        cursor: Optional[str] = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=100),
        principal: SessionPrincipal = Depends(require_conversation_session),
    ) -> dict:
        items = _run_domain(
            lambda: service().list_conversations(
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
                "next_cursor": (
                    items[-1].conversation_id if len(items) == limit else None
                ),
            },
        )

    @router.get(
        "/ai-conversations/{conversation_id}/messages",
        response_model=MingchanConversationMessagesResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def list_conversation_messages(
        conversation_id: str,
        request: Request,
        response: Response,
        cursor: Optional[int] = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
        principal: SessionPrincipal = Depends(require_conversation_session),
    ) -> dict:
        target, messages = _run_domain(
            lambda: service().list_conversation_messages(
                principal.platform_user_id,
                conversation_id,
                before_id=cursor,
                limit=limit,
            )
        )
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
                    _ai_message_data(
                        item,
                        assets=assets,
                        scope=scope,
                        ttl_seconds=ttl,
                    )
                    for item in messages
                ],
                "next_cursor": messages[0].id if len(messages) == limit else None,
            },
        )

    @router.post(
        "/ai-conversations/{conversation_id}/read",
        response_model=MingchanConversationReadResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def mark_conversation_read(
        conversation_id: str,
        payload: MingchanConversationReadRequest,
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_conversation_session),
    ) -> dict:
        state = _run_domain(
            lambda: service().mark_conversation_read(
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
        response_model=MingchanConversationTurnResponse,
        responses=WORLD_ERROR_RESPONSES,
    )
    def conversation_turn(
        conversation_id: str,
        payload: MingchanConversationTurnRequest,
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_conversation_session),
    ) -> dict:
        clean_text = payload.text.strip()
        clean_media_ref = (payload.media_ref or "").strip() or None
        if clean_media_ref:
            media_asset = resolve_chat_media(
                media_ref=clean_media_ref,
                platform_user_id=principal.platform_user_id,
            )
        elif not clean_text:
            raise MingchanWorldApiError("media_content_required")
        else:
            media_asset = None
        conversation_service = service()
        target = _run_domain(
            lambda: conversation_service.resolve_conversation(
                principal.platform_user_id,
                conversation_id,
            )
        )
        mapped_message_id = (
            f"app:{target.conversation_id}:{payload.client_message_id}"
        )
        duplicate = get_duplicate_reply_record(
            account_id=target.runtime_account_id,
            reply_to_message_id=mapped_message_id,
        )
        if duplicate is not None:
            _no_store(response)
            return _envelope(
                request,
                code="ok",
                data=_turn_data(duplicate),
            )

        with try_conversation_turn_lock(conversation_id) as acquired:
            if not acquired:
                raise MingchanWorldApiError("turn_in_progress")
            target = _run_domain(
                lambda: conversation_service.resolve_conversation(
                    principal.platform_user_id,
                    conversation_id,
                )
            )
            if target.state != "active":
                raise MingchanWorldApiError("conversation_read_only")
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
                    result = run_mingchan_companion_world_turn(
                        conversation_id=target.conversation_id,
                        universe_id=target.universe_id,
                        resident_id=target.resident_id,
                        runtime_account_id=target.runtime_account_id,
                        platform_user_id=principal.platform_user_id,
                        sender_name=platform_user.get("display_name"),
                        message_id=mapped_message_id,
                        registry=registry,
                        **_turn_media_kwargs(
                            asset=media_asset,
                            caption=clean_text,
                            platform_user_id=principal.platform_user_id,
                        ),
                    )
                except MediaRefInvalidError as err:
                    raise MingchanWorldApiError("media_ref_invalid") from err

        if result is None:
            data = _turn_data(duplicate)
        else:
            if result.status == "rate_limited":
                raise MingchanWorldApiError("rate_limited")
            if result.status == "disabled":
                raise MingchanWorldApiError("account_disabled")
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

    return router


__all__ = ["build_router"]
