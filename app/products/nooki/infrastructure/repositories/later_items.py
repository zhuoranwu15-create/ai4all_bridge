"""Nooki 稍后盒子 persistence；所有读写以 platform_user_id 为隔离锚。"""
from __future__ import annotations

import uuid
from typing import Optional

from app.db import connect


def _public_item(row) -> dict:
    return {
        "later_item_id": row["id"],
        "content": row["content"],
        "status": row["status"],
        "converted_task_id": row["converted_task_id"],
        "version": int(row["version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
    }


class LaterItemNotFoundError(Exception):
    """稍后项不存在，或不属于当前用户。"""


class LaterItemVersionConflictError(Exception):
    """客户端基于旧版本修改稍后项。"""


class NookiLaterItemRepository:
    """提供稍后项幂等创建、列表、改名和归档。"""

    def create(
        self,
        *,
        platform_user_id: str,
        content: str,
        client_request_id: str,
        source_message_id: Optional[str] = None,
    ) -> tuple[dict, bool]:
        with connect() as conn:
            item_id = f"later_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO nooki_later_items(
                    id, platform_user_id, content, source_message_id, client_request_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform_user_id, client_request_id) DO NOTHING
                """,
                (
                    item_id,
                    platform_user_id,
                    content,
                    source_message_id,
                    client_request_id,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM nooki_later_items
                WHERE platform_user_id = ? AND client_request_id = ?
                """,
                (platform_user_id, client_request_id),
            ).fetchone()
        return _public_item(row), row["id"] != item_id

    def list_items(self, *, platform_user_id: str, status: str = "inbox") -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM nooki_later_items
                WHERE platform_user_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                """,
                (platform_user_id, status),
            ).fetchall()
        return [_public_item(row) for row in rows]

    def update_content(
        self,
        *,
        platform_user_id: str,
        item_id: str,
        content: str,
        expected_version: int,
    ) -> dict:
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE nooki_later_items
                SET content = ?, version = version + 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND platform_user_id = ? AND status = 'inbox'
                  AND version = ?
                """,
                (content, item_id, platform_user_id, expected_version),
            )
            row = conn.execute(
                "SELECT * FROM nooki_later_items WHERE id = ? AND platform_user_id = ?",
                (item_id, platform_user_id),
            ).fetchone()
            if cursor.rowcount == 0:
                self._raise_write_error(row, expected_version)
        return _public_item(row)

    def archive(
        self, *, platform_user_id: str, item_id: str, expected_version: int
    ) -> dict:
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE nooki_later_items
                SET status = 'archived', version = version + 1,
                    archived_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND platform_user_id = ? AND status = 'inbox'
                  AND version = ?
                """,
                (item_id, platform_user_id, expected_version),
            )
            row = conn.execute(
                "SELECT * FROM nooki_later_items WHERE id = ? AND platform_user_id = ?",
                (item_id, platform_user_id),
            ).fetchone()
            if cursor.rowcount == 0:
                self._raise_write_error(row, expected_version)
        return _public_item(row)

    @staticmethod
    def _raise_write_error(row, expected_version: int) -> None:
        if row is None or row["status"] != "inbox":
            raise LaterItemNotFoundError()
        if int(row["version"]) != expected_version:
            raise LaterItemVersionConflictError()
        raise LaterItemNotFoundError()


__all__ = [
    "LaterItemNotFoundError",
    "LaterItemVersionConflictError",
    "NookiLaterItemRepository",
]
