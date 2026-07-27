"""Nooki 小程序 App API：人设设置、自然语言聊天入口、状态投影。

按钮态的确定性操作（选方案/开始/完成）在 `api/tasks.py`，不走这里——那一路不经过 LLM。
"""
from __future__ import annotations

import re
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from app.agent_runtime.turns.service import ChannelTurnInput
from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import (
    SessionPrincipal,
    get_active_bound_account_for_user_in_app,
    get_duplicate_reply,
    get_or_create_default_ai4all_account_for_user,
    get_platform_user,
)
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.products.nooki.application.persona import DEFAULT_ARCHETYPE, DEFAULT_COMPANION_NAME
from app.products.nooki.application.turns import run_nooki_turn
from app.products.nooki.application.ui_projection import project_task_view
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository
from app.products.nooki.infrastructure.repositories.conversation import (
    NookiConversationRepository,
)
from app.products.nooki.infrastructure.repositories.user_profile import (
    get_explicit_preferences,
    set_explicit_preferences,
)
from app.products.nooki.infrastructure.repositories.later_items import (
    NookiLaterItemRepository,
)
from app.routers.deps import require_product_session
from app.time_utils import beijing_now

router = APIRouter(tags=["nooki-app"])

_require_nooki_session = require_product_session(NOOKI_APP_ID)

_CLIENT_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_VALID_ARCHETYPES = {"gentle", "calm", "bestie"}

_turn_locks_guard = threading.Lock()
_turn_locks: dict[str, threading.Lock] = {}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _turn_lock(account_id: str) -> threading.Lock:
    with _turn_locks_guard:
        lock = _turn_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _turn_locks[account_id] = lock
        return lock


def _account_id_for_principal(principal: SessionPrincipal) -> str:
    """一人一个默认 Nooki runtime account；不存在则懒创建（人设/对话历史锚点，不落任务数据）。"""

    result = get_active_bound_account_for_user_in_app(
        platform_user_id=principal.platform_user_id, app_id=NOOKI_APP_ID
    )
    if result is None:
        result = get_or_create_default_ai4all_account_for_user(
            platform_user_id=principal.platform_user_id,
            display_name=None,
            plan="free",
            campaign_code=None,
            initial_channel=CHANNEL_APP,
            binding_method="wx_onboarding",
            app_id=NOOKI_APP_ID,
        )
    return result["account"]["id"]


def _authoritative_view(
    platform_user_id: str, *, source_message_id: Optional[str] = None
) -> tuple[dict, list[dict]]:
    """每次响应都重新读取数据库；重复聊天也不复用旧卡片。"""

    service = GoalBreakdownService(SqlTaskRepository())
    focus_task_id = None
    if source_message_id:
        focus_task_id = service.get_focus_task_id_for_source_message(
            platform_user_id=platform_user_id,
            source_message_id=source_message_id,
        )
    projection = service.get_authoritative_state(platform_user_id, focus_task_id)
    return project_task_view(projection)


class CompanionRequest(BaseModel):
    archetype: str = Field(min_length=1, max_length=32)
    companion_name: Optional[str] = Field(default=None, max_length=32)

    @field_validator("archetype")
    @classmethod
    def validate_archetype(cls, value: str) -> str:
        if value not in _VALID_ARCHETYPES:
            raise ValueError("invalid archetype")
        return value

    @field_validator("companion_name")
    @classmethod
    def clean_companion_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class NookiTurnRequest(BaseModel):
    conversation_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(min_length=8, max_length=64)

    @field_validator("text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        return cleaned

    @field_validator("client_message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        if not _CLIENT_MESSAGE_ID_RE.fullmatch(value):
            raise ValueError("invalid client_message_id")
        return value


def _owned_conversation(*, principal: SessionPrincipal, conversation_id: str) -> dict:
    conversation = NookiConversationRepository().get_owned(
        conversation_id=conversation_id,
        platform_user_id=principal.platform_user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return conversation


def _conversation_view(conversation: dict) -> dict:
    return {
        "conversation_id": conversation["id"],
        "status": conversation["status"],
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "last_message_at": conversation["last_message_at"],
    }


@router.post("/profile/companion")
def nooki_set_companion(
    payload: CompanionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    merged = set_explicit_preferences(
        principal.platform_user_id,
        archetype=payload.archetype,
        companion_name=payload.companion_name,
    )
    _no_store(response)
    return {
        "status": "ok",
        "archetype": merged.get("archetype") or DEFAULT_ARCHETYPE,
        "companion_name": merged.get("companion_name") or DEFAULT_COMPANION_NAME,
    }


@router.get("/profile/companion")
def nooki_get_companion(
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    preferences = get_explicit_preferences(principal.platform_user_id)
    _no_store(response)
    return {
        "status": "ok",
        "archetype": preferences.get("archetype") or DEFAULT_ARCHETYPE,
        "companion_name": preferences.get("companion_name") or DEFAULT_COMPANION_NAME,
    }


@router.get("/state")
def nooki_state(
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    state, cards = _authoritative_view(principal.platform_user_id)
    _no_store(response)
    return {"status": "ok", "reply": None, "state": state, "cards": cards, "metadata": {}}


@router.get("/bootstrap")
def nooki_bootstrap(
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    """恢复小程序首屏所需的 Conversation、近期消息、Profile 与权威任务投影。"""

    account_id = _account_id_for_principal(principal)
    conversations = NookiConversationRepository()
    conversation = conversations.get_or_create_active(
        platform_user_id=principal.platform_user_id,
        runtime_account_id=account_id,
    )
    messages, has_more = conversations.list_messages_before(
        conversation=conversation, before_id=None, limit=50
    )
    preferences = get_explicit_preferences(principal.platform_user_id)
    state, cards = _authoritative_view(principal.platform_user_id)
    _no_store(response)
    return {
        "status": "ok",
        "conversation": _conversation_view(conversation),
        "messages": messages,
        "next_cursor": messages[0]["cursor"] if has_more and messages else None,
        "has_more": has_more,
        "server_cursor": conversations.latest_cursor(conversation=conversation),
        "server_time": beijing_now().isoformat(),
        "profile": {
            "archetype": preferences.get("archetype") or DEFAULT_ARCHETYPE,
            "companion_name": preferences.get("companion_name") or DEFAULT_COMPANION_NAME,
        },
        "state": state,
        "cards": cards,
        "later_items": NookiLaterItemRepository().list_items(
            platform_user_id=principal.platform_user_id
        ),
    }


@router.get("/conversations/{conversation_id}/messages")
def nooki_messages(
    conversation_id: str,
    response: Response,
    before_cursor: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    """按长期 Conversation 跨 Runtime Session 倒序翻页，页内保持时间正序。"""

    conversation = _owned_conversation(
        principal=principal, conversation_id=conversation_id
    )
    messages, has_more = NookiConversationRepository().list_messages_before(
        conversation=conversation,
        before_id=before_cursor,
        limit=limit,
    )
    _no_store(response)
    return {
        "status": "ok",
        "messages": messages,
        "next_cursor": messages[0]["cursor"] if has_more and messages else None,
        "has_more": has_more,
    }


@router.get("/sync")
def nooki_sync(
    conversation_id: str,
    response: Response,
    after_cursor: Optional[int] = Query(default=None, ge=0),
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    """返回游标之后的新消息与当前权威状态；客户端不得反向覆盖服务端。"""

    conversation = _owned_conversation(
        principal=principal, conversation_id=conversation_id
    )
    conversations = NookiConversationRepository()
    messages, has_more = conversations.list_messages_after(
        conversation=conversation,
        after_id=after_cursor,
    )
    state, cards = _authoritative_view(principal.platform_user_id)
    _no_store(response)
    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "messages": messages,
        "state": state,
        "cards": cards,
        "later_items": NookiLaterItemRepository().list_items(
            platform_user_id=principal.platform_user_id
        ),
        "has_more": has_more,
        "server_cursor": conversations.latest_cursor(conversation=conversation),
        "server_time": beijing_now().isoformat(),
    }


@router.post("/chat")
def nooki_chat(
    payload: NookiTurnRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    conversation = _owned_conversation(
        principal=principal, conversation_id=payload.conversation_id
    )
    account_id = conversation["runtime_account_id"]
    platform_user = get_platform_user(platform_user_id=principal.platform_user_id) or {}
    mapped_message_id = f"nooki:{conversation['id']}:{payload.client_message_id}"

    duplicate_reply = get_duplicate_reply(
        account_id=account_id, reply_to_message_id=mapped_message_id
    )
    if duplicate_reply is not None:
        state, cards = _authoritative_view(
            principal.platform_user_id, source_message_id=mapped_message_id
        )
        _no_store(response)
        user_message, assistant_message = NookiConversationRepository().get_message_pair(
            conversation=conversation, inbound_message_id=mapped_message_id
        )
        return {
            "status": "ok",
            "reply": duplicate_reply,
            "no_reply": False,
            "state": state,
            "cards": cards,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "server_cursor": NookiConversationRepository().latest_cursor(
                conversation=conversation
            ),
            "metadata": {"deduplicated": True, "message_id": mapped_message_id},
        }

    lock = _turn_lock(account_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="turn_in_progress")
    try:
        duplicate_reply = get_duplicate_reply(
            account_id=account_id, reply_to_message_id=mapped_message_id
        )
        if duplicate_reply is not None:
            state, cards = _authoritative_view(
                principal.platform_user_id, source_message_id=mapped_message_id
            )
            _no_store(response)
            user_message, assistant_message = NookiConversationRepository().get_message_pair(
                conversation=conversation, inbound_message_id=mapped_message_id
            )
            return {
                "status": "ok",
                "reply": duplicate_reply,
                "no_reply": False,
                "state": state,
                "cards": cards,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "server_cursor": NookiConversationRepository().latest_cursor(
                    conversation=conversation
                ),
                "metadata": {"deduplicated": True, "message_id": mapped_message_id},
            }

        identity = ResolvedIdentity(
            ai4all_account_id=account_id,
            session_key=f"nooki:{principal.platform_user_id}",
            channel=CHANNEL_APP,
            channel_account_id=principal.platform_user_id,
            sender_id=principal.platform_user_id,
            chat_id=None,
        )
        result = run_nooki_turn(
            ChannelTurnInput(
                account_id=account_id,
                app_id=principal.app_id,
                cap=CHANNELS[CHANNEL_APP],
                identity=identity,
                message_id=mapped_message_id,
                event_id=None,
                message_type="text",
                text=payload.text,
                media=None,
                raw={"source": "nooki_app_v1"},
                sender_name=platform_user.get("display_name"),
            )
        )
    finally:
        lock.release()

    if result.status == "rate_limited":
        raise HTTPException(status_code=429, detail=result.reply or "rate_limited")
    if result.status == "disabled":
        raise HTTPException(status_code=403, detail="account_disabled")
    state, cards = _authoritative_view(
        principal.platform_user_id, source_message_id=mapped_message_id
    )
    conversations = NookiConversationRepository()
    conversations.touch(conversation_id=conversation["id"])
    user_message, assistant_message = conversations.get_message_pair(
        conversation=conversation, inbound_message_id=mapped_message_id
    )
    _no_store(response)
    return {
        "status": "ok" if result.status == "duplicate" else result.status,
        "reply": result.reply,
        "no_reply": result.no_reply,
        "state": state,
        "cards": cards,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "server_cursor": conversations.latest_cursor(conversation=conversation),
        "metadata": {
            "deduplicated": result.status == "duplicate",
            "message_id": result.metadata.get("reply_message_id"),
        },
    }


__all__ = ["router"]
