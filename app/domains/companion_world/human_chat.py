"""Companion World M5 真人一对一聊天纯领域 DTO 与状态规则。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

HUMAN_CONVERSATION_STATUSES = frozenset({"active", "read_only"})
HUMAN_REPORT_STATUSES = frozenset({"open", "reviewed", "closed"})


@dataclass(frozen=True)
class HumanConversationRecord:
    """一条与 visit 一对一绑定的真人会话。"""

    id: str
    visit_id: str
    owner_platform_user_id: str
    visitor_platform_user_id: str
    status: str
    created_at: str
    last_message_at: Optional[str] = None
    owner_hidden_at: Optional[str] = None
    visitor_hidden_at: Optional[str] = None


@dataclass(frozen=True)
class HumanMessageRecord:
    """永不进入 AI message/runtime 的真人文字消息。"""

    id: str
    conversation_id: str
    sender_platform_user_id: str
    client_message_id: str
    body_text: str
    created_at: str


def human_conversation_transition_allowed(
    current_status: str, target_status: str
) -> bool:
    """真人会话只允许从 active 单向进入 read_only。"""
    return current_status == target_status or (
        current_status == "active" and target_status == "read_only"
    )


def normalize_human_message_body(body_text: str, *, max_chars: int = 4000) -> str:
    """校验并返回首版真人纯文字正文，不接受空白或超长内容。"""
    cleaned = str(body_text or "").strip()
    if not cleaned:
        raise ValueError("human message body is required")
    if len(cleaned) > max_chars:
        raise ValueError("human message body is too long")
    return cleaned


def human_message_fingerprint(*, client_message_id: str, body_text: str) -> str:
    """生成 sender-scoped idempotency 对账指纹。"""
    payload = f"{client_message_id}\n{body_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HumanChatRepository(Protocol):
    """M5 真人聊天持久化端口；participant 身份必须来自 session。"""

    def get_conversation_for_participant(
        self, *, conversation_id: str, platform_user_id: str
    ) -> Optional[HumanConversationRecord]: ...

    def list_messages_for_participant(
        self,
        *,
        conversation_id: str,
        platform_user_id: str,
        limit: int,
    ) -> Sequence[HumanMessageRecord]: ...


__all__ = [
    "HUMAN_CONVERSATION_STATUSES",
    "HUMAN_REPORT_STATUSES",
    "HumanChatRepository",
    "HumanConversationRecord",
    "HumanMessageRecord",
    "human_conversation_transition_allowed",
    "human_message_fingerprint",
    "normalize_human_message_body",
]
