"""Nooki 长期 Conversation 与跨 Runtime Session 的用户可见消息读取。"""
from __future__ import annotations

import uuid
from typing import Optional

from app.db import APP_ACTIVE_SESSION_KEY, connect
from app.db._backend import IntegrityError


def _new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex}"


def _public_message(row) -> dict:
    """把 Runtime message 投影为小程序可见消息，不暴露 account/session 标识。"""

    return {
        "message_id": row["message_id"] or f"msg_{row['id']}",
        "reply_to_message_id": row["reply_to_message_id"],
        "role": row["role"],
        "message_type": row["message_type"],
        "content": row["content"],
        "created_at": row["created_at"],
        "cursor": str(row["id"]),
    }


class NookiConversationRepository:
    """按 platform user 校验 Conversation，并读取其 App 会话时间线。"""

    def get_or_create_active(
        self, *, platform_user_id: str, runtime_account_id: str
    ) -> dict:
        existing = self.get_active(platform_user_id=platform_user_id)
        if existing is not None:
            if existing["runtime_account_id"] != runtime_account_id:
                raise RuntimeError("nooki_conversation_runtime_account_mismatch")
            return existing
        try:
            with connect() as conn:
                conversation_id = _new_conversation_id()
                conn.execute(
                    """
                    INSERT INTO nooki_conversations(
                        id, platform_user_id, runtime_account_id, status
                    ) VALUES (?, ?, ?, 'active')
                    """,
                    (conversation_id, platform_user_id, runtime_account_id),
                )
        except IntegrityError:
            # 并发 bootstrap 可能同时创建；唯一约束决定赢家，重新读取即可。
            pass
        conversation = self.get_active(platform_user_id=platform_user_id)
        if conversation is None:
            raise RuntimeError("nooki_conversation_create_failed")
        if conversation["runtime_account_id"] != runtime_account_id:
            raise RuntimeError("nooki_conversation_runtime_account_mismatch")
        return conversation

    def get_active(self, *, platform_user_id: str) -> Optional[dict]:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM nooki_conversations
                WHERE platform_user_id = ? AND status = 'active'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (platform_user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_owned(self, *, conversation_id: str, platform_user_id: str) -> Optional[dict]:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM nooki_conversations
                WHERE id = ? AND platform_user_id = ? AND status = 'active'
                """,
                (conversation_id, platform_user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_messages_before(
        self, *, conversation: dict, before_id: Optional[int], limit: int
    ) -> tuple[list[dict], bool]:
        rows = self._message_rows(
            conversation=conversation,
            cursor_id=before_id,
            direction="before",
            limit=limit,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        return [_public_message(row) for row in reversed(page)], has_more

    def list_messages_after(
        self, *, conversation: dict, after_id: Optional[int], limit: int = 100
    ) -> tuple[list[dict], bool]:
        rows = self._message_rows(
            conversation=conversation,
            cursor_id=after_id,
            direction="after",
            limit=limit,
        )
        has_more = len(rows) > limit
        return [_public_message(row) for row in rows[:limit]], has_more

    def _message_rows(
        self,
        *,
        conversation: dict,
        cursor_id: Optional[int],
        direction: str,
        limit: int,
    ) -> list:
        """读取一个 Conversation 的用户/助手文本；account 与 App session scope 双重约束。"""

        runtime_account_id = conversation["runtime_account_id"]
        prefix = f"{APP_ACTIVE_SESSION_KEY}:"
        comparison = "m.id < ?" if direction == "before" else "m.id > ?"
        order = "DESC" if direction == "before" else "ASC"
        cursor_clause = f"AND {comparison}" if cursor_id is not None else ""
        params = [
            runtime_account_id,
            runtime_account_id,
            APP_ACTIVE_SESSION_KEY,
            len(prefix),
            prefix,
        ]
        if cursor_id is not None:
            params.append(cursor_id)
        params.append(limit + 1)
        with connect() as conn:
            return conn.execute(
                f"""
                SELECT m.id, m.message_id, m.reply_to_message_id, m.role,
                       m.message_type, m.content, m.created_at
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE m.account_id = ? AND s.account_id = ?
                  AND (s.session_key = ? OR substr(s.session_key, 1, ?) = ?)
                  AND m.role IN ('user', 'assistant')
                  AND m.content IS NOT NULL AND m.content != ''
                  {cursor_clause}
                ORDER BY m.id {order}
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()

    def get_message_pair(self, *, conversation: dict, inbound_message_id: str) -> tuple:
        runtime_account_id = conversation["runtime_account_id"]
        with connect() as conn:
            inbound = conn.execute(
                """
                SELECT id, message_id, reply_to_message_id, role, message_type, content, created_at
                FROM messages WHERE account_id = ? AND message_id = ? AND role = 'user'
                ORDER BY id DESC LIMIT 1
                """,
                (runtime_account_id, inbound_message_id),
            ).fetchone()
            outbound = conn.execute(
                """
                SELECT id, message_id, reply_to_message_id, role, message_type, content, created_at
                FROM messages WHERE account_id = ? AND reply_to_message_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (runtime_account_id, inbound_message_id),
            ).fetchone()
        return (
            _public_message(inbound) if inbound else None,
            _public_message(outbound) if outbound else None,
        )

    def latest_cursor(self, *, conversation: dict) -> Optional[str]:
        runtime_account_id = conversation["runtime_account_id"]
        with connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(id) AS id FROM messages
                WHERE account_id = ? AND role IN ('user', 'assistant')
                """,
                (runtime_account_id,),
            ).fetchone()
        return str(row["id"]) if row and row["id"] is not None else None

    def touch(self, *, conversation_id: str) -> None:
        with connect() as conn:
            conn.execute(
                """
                UPDATE nooki_conversations
                SET last_message_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (conversation_id,),
            )
