"""鸣蝉居民会话的 SQLite/PostgreSQL repository adapter。"""
from __future__ import annotations

from typing import Optional, Sequence

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db._core import connect
from app.db.accounts import list_app_conversation_messages_before
from app.db.product_memberships import _require_active_product_membership_in_conn
from app.products.mingchan.domain.companion_world.conversations import (
    ConversationMessage,
    ConversationReadState,
    ConversationSummary,
    ConversationTarget,
    MingchanConversationRepository,
)
from app.products.mingchan.infrastructure.persistence import companion_world as world_db


class SqlMingchanConversationRepository(MingchanConversationRepository):
    """把鸣蝉会话端口映射到共享表，并固定 membership/account 产品边界。"""

    def __init__(
        self,
        *,
        registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    ) -> None:
        self._registry = registry

    def _require_owner(self, conn, platform_user_id: str) -> None:
        _require_active_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
            registry=self._registry,
        )

    def resolve_conversation_for_owner(
        self,
        conversation_id: str,
        platform_user_id: str,
    ) -> Optional[ConversationTarget]:
        """按 owner 和 ``app_id=mingchan`` 解析居民会话。"""

        with connect() as conn:
            self._require_owner(conn, platform_user_id)
            row = world_db.resolve_conversation_for_owner(
                conversation_id=conversation_id,
                owner_platform_user_id=platform_user_id,
                expected_app_id=MINGCHAN_APP_ID,
                conn=conn,
            )
        if row is None:
            return None
        return ConversationTarget(
            conversation_id=str(row["conversation_id"]),
            universe_id=str(row["universe_id"]),
            resident_id=str(row["resident_id"]),
            owner_platform_user_id=str(row["owner_platform_user_id"]),
            runtime_account_id=str(row["runtime_account_id"]),
            state=str(row["state"]),
        )

    def list_conversations_for_owner(
        self,
        platform_user_id: str,
        cursor_conversation_id: Optional[str],
        limit: int,
    ) -> Sequence[ConversationSummary]:
        """列出 owner 的鸣蝉会话并批量投影预览与未读数。"""

        with connect() as conn:
            self._require_owner(conn, platform_user_id)
            rows = world_db.list_conversations_for_owner(
                owner_platform_user_id=platform_user_id,
                cursor_conversation_id=cursor_conversation_id,
                limit=limit,
                expected_app_id=MINGCHAN_APP_ID,
                conn=conn,
            )
        return tuple(
            ConversationSummary(
                conversation_id=str(row["conversation_id"]),
                resident_id=str(row["resident_id"]),
                resident_name=str(row["resident_name"]),
                resident_avatar_ref=row.get("avatar_ref"),
                resident_status=str(row["resident_status"]),
                state=str(row["state"]),
                last_preview=row.get("last_preview"),
                unread=int(row.get("unread") or 0),
                last_message_at=row.get("last_message_at"),
                sort_time=row.get("updated_at"),
            )
            for row in rows
        )

    def list_conversation_messages(
        self,
        runtime_account_id: str,
        before_id: Optional[int],
        limit: int,
    ) -> Optional[Sequence[ConversationMessage]]:
        """在再次校验 runtime account 产品后读取 App scope 历史。"""

        with connect() as conn:
            account = conn.execute(
                "SELECT app_id FROM accounts WHERE id = ?",
                (runtime_account_id,),
            ).fetchone()
            if account is None or str(account["app_id"]) != MINGCHAN_APP_ID:
                return None
            rows = list_app_conversation_messages_before(
                runtime_account_id=runtime_account_id,
                before_id=before_id,
                limit=limit,
                conn=conn,
            )
        return tuple(
            ConversationMessage(
                id=int(row["id"]),
                message_id=row.get("message_id"),
                role=str(row["role"]),
                message_type=str(row["message_type"]),
                content=str(row["content"]),
                created_at=str(row["created_at"]),
                content_json=row.get("content_json"),
                media_id=row.get("media_id"),
            )
            for row in rows
        )

    def advance_conversation_read_cursor(
        self,
        conversation_id: str,
        platform_user_id: str,
        last_message_id: int,
    ) -> Optional[ConversationReadState]:
        """只推进 owner 的鸣蝉会话游标，产品错配按不存在处理。"""

        with connect() as conn:
            self._require_owner(conn, platform_user_id)
            row = world_db.advance_conversation_read_cursor(
                conversation_id=conversation_id,
                owner_platform_user_id=platform_user_id,
                last_message_id=last_message_id,
                expected_app_id=MINGCHAN_APP_ID,
                conn=conn,
            )
        if row is None:
            return None
        cursor = row.get("last_read_message_id")
        return ConversationReadState(
            last_read_message_id=int(cursor) if cursor else None,
            unread=int(row.get("unread") or 0),
        )


__all__ = ["SqlMingchanConversationRepository"]
