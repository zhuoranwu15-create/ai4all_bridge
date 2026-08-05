"""鸣蝉居民会话的领域契约与 owner-scoped 服务。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple


class MingchanConversationError(Exception):
    """可稳定映射到鸣蝉 HTTP 错误码的会话领域错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ConversationTarget:
    """owner 与产品归属均已校验的居民会话目标。"""

    conversation_id: str
    universe_id: str
    resident_id: str
    owner_platform_user_id: str
    runtime_account_id: str
    state: str


@dataclass(frozen=True)
class ConversationSummary:
    """会话列表中的客户端安全摘要。"""

    conversation_id: str
    resident_id: str
    resident_name: str
    resident_avatar_ref: Optional[str]
    resident_status: str
    state: str
    last_preview: Optional[str]
    unread: int
    last_message_at: Optional[str] = None
    sort_time: Optional[str] = None

    @property
    def can_send(self) -> bool:
        """返回当前会话是否允许发送新消息。"""

        return self.state == "active"

    @property
    def read_only_reason(self) -> Optional[str]:
        """返回稳定只读原因码；可发送时为 ``None``。"""

        return None if self.can_send else "resident_offline"


@dataclass(frozen=True)
class ConversationMessage:
    """一个居民 runtime account 的 App scope 可见消息。"""

    id: int
    message_id: Optional[str]
    role: str
    message_type: str
    content: str
    created_at: str
    content_json: Optional[str] = None
    media_id: Optional[str] = None


@dataclass(frozen=True)
class ConversationReadState:
    """已读游标推进后的状态。"""

    last_read_message_id: Optional[int]
    unread: int


class MingchanConversationRepository(Protocol):
    """鸣蝉会话服务所需的最小 persistence 端口。"""

    def resolve_conversation_for_owner(
        self, conversation_id: str, platform_user_id: str
    ) -> Optional[ConversationTarget]: ...

    def list_conversations_for_owner(
        self,
        platform_user_id: str,
        cursor_conversation_id: Optional[str],
        limit: int,
    ) -> Sequence[ConversationSummary]: ...

    def list_conversation_messages(
        self,
        runtime_account_id: str,
        before_id: Optional[int],
        limit: int,
    ) -> Optional[Sequence[ConversationMessage]]: ...

    def advance_conversation_read_cursor(
        self,
        conversation_id: str,
        platform_user_id: str,
        last_message_id: int,
    ) -> Optional[ConversationReadState]: ...


class MingchanConversationService:
    """鸣蝉居民会话的 owner-scoped 领域服务。"""

    def __init__(self, repository: MingchanConversationRepository) -> None:
        self._repository = repository

    def resolve_conversation(
        self, platform_user_id: str, conversation_id: str
    ) -> ConversationTarget:
        """解析本人的鸣蝉会话；不存在、越权与产品错配统一收敛。"""

        target = self._repository.resolve_conversation_for_owner(
            conversation_id,
            platform_user_id,
        )
        if target is None:
            raise MingchanConversationError("conversation_not_found")
        return target

    def list_conversations(
        self,
        platform_user_id: str,
        *,
        cursor_conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[ConversationSummary, ...]:
        """列出本人且仅属于鸣蝉的居民会话。"""

        return tuple(
            self._repository.list_conversations_for_owner(
                platform_user_id,
                cursor_conversation_id,
                limit,
            )
        )

    def list_conversation_messages(
        self,
        platform_user_id: str,
        conversation_id: str,
        *,
        before_id: Optional[int] = None,
        limit: int = 50,
    ) -> Tuple[ConversationTarget, Tuple[ConversationMessage, ...]]:
        """校验 owner 与产品后读取目标居民的 App scope 历史。"""

        target = self.resolve_conversation(platform_user_id, conversation_id)
        messages = self._repository.list_conversation_messages(
            target.runtime_account_id,
            before_id,
            limit,
        )
        if messages is None:
            raise MingchanConversationError("conversation_not_found")
        return target, tuple(messages)

    def mark_conversation_read(
        self,
        platform_user_id: str,
        conversation_id: str,
        *,
        last_message_id: int,
    ) -> ConversationReadState:
        """单调推进本人鸣蝉会话的已读游标。"""

        state = self._repository.advance_conversation_read_cursor(
            conversation_id,
            platform_user_id,
            last_message_id,
        )
        if state is None:
            raise MingchanConversationError("conversation_not_found")
        return state


__all__ = [
    "ConversationMessage",
    "ConversationReadState",
    "ConversationSummary",
    "ConversationTarget",
    "MingchanConversationError",
    "MingchanConversationRepository",
    "MingchanConversationService",
]
