"""M5 独立真人会话/拉黑的 owner/visitor 锚定存储原语。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "create_human_conversation_for_visit",
    "get_human_conversation_for_participant",
    "get_human_conversation_for_visit",
    "has_platform_user_block",
    "insert_platform_user_block",
    "list_human_conversations_for_participant",
]


@contextmanager
def _human_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用外层事务；独立 SQLite 写使用 IMMEDIATE。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        if not is_postgres():
            own.execute("BEGIN IMMEDIATE")
        yield own


def create_human_conversation_for_visit(
    *,
    visit_id: str,
    owner_platform_user_id: str,
    visitor_platform_user_id: str,
    created_at: str,
    conversation_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """为已锁定的 visit 幂等创建一条一对一真人会话。"""
    if not visit_id or not owner_platform_user_id or not visitor_platform_user_id:
        raise ValueError("human conversation anchors are required")
    if owner_platform_user_id == visitor_platform_user_id:
        raise ValueError("human conversation participants must differ")
    conversation_id = conversation_id or _new_id("hconv")
    suffix = " FOR UPDATE" if is_postgres() else ""
    with _human_write_tx(conn) as tx:
        visit = tx.execute(
            "SELECT id, owner_platform_user_id, visitor_platform_user_id, status "
            "FROM universe_visits WHERE id = ?" + suffix,
            (visit_id,),
        ).fetchone()
        if visit is None:
            raise ValueError("human conversation visit not found")
        if (
            visit["owner_platform_user_id"] != owner_platform_user_id
            or visit["visitor_platform_user_id"] != visitor_platform_user_id
        ):
            raise ValueError("human conversation visit ownership mismatch")
        if visit["status"] != "active":
            raise ValueError("human conversation requires active visit")
        existing = tx.execute(
            "SELECT * FROM human_conversations WHERE visit_id = ?", (visit_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        tx.execute(
            """
            INSERT INTO human_conversations(
                id, visit_id, owner_platform_user_id, visitor_platform_user_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                visit_id,
                owner_platform_user_id,
                visitor_platform_user_id,
                "active",
                created_at,
                created_at,
            ),
        )
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("human conversation was not created")
    return dict(row)


def get_human_conversation_for_participant(
    *,
    conversation_id: str,
    platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 conversation + participant 读取；第三方返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE id = ? "
            "AND (owner_platform_user_id = ? OR visitor_platform_user_id = ?)",
            (conversation_id, platform_user_id, platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_human_conversation_for_visit(
    *, visit_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """内部按 visit 读取会话；公开 API 必须使用 participant-scoped 查询。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE visit_id = ?", (visit_id,)
        ).fetchone()
    return dict(row) if row else None


def list_human_conversations_for_participant(
    *,
    platform_user_id: str,
    include_hidden: bool = False,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出 participant 会话；默认按各自 hidden 字段过滤。"""
    hidden = "" if include_hidden else (
        " AND ((owner_platform_user_id = ? AND owner_hidden_at IS NULL) "
        "OR (visitor_platform_user_id = ? AND visitor_hidden_at IS NULL))"
    )
    params: list[Any] = [platform_user_id, platform_user_id]
    if not include_hidden:
        params.extend([platform_user_id, platform_user_id])
    params.append(max(1, min(int(limit), 100)))
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT * FROM human_conversations WHERE "
            "(owner_platform_user_id = ? OR visitor_platform_user_id = ?)"
            + hidden
            + " ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_platform_user_block(
    *,
    blocker_platform_user_id: str,
    blocked_platform_user_id: str,
    created_at: str,
    conn: Optional[Connection] = None,
) -> bool:
    """幂等写入单向 block；任一方向存在即由上层关闭双方 contact ACL。"""
    if not blocker_platform_user_id or not blocked_platform_user_id:
        raise ValueError("block participants are required")
    if blocker_platform_user_id == blocked_platform_user_id:
        raise ValueError("cannot block self")
    with _human_write_tx(conn) as tx:
        inserted = tx.execute(
            "INSERT INTO platform_user_blocks("
            "blocker_platform_user_id, blocked_platform_user_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (blocker_platform_user_id, blocked_platform_user_id, created_at),
        )
    return inserted.rowcount == 1


def has_platform_user_block(
    *,
    first_platform_user_id: str,
    second_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> bool:
    """返回任一方向是否存在 block；用于 fail-closed contact ACL。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT 1 FROM platform_user_blocks WHERE "
            "(blocker_platform_user_id = ? AND blocked_platform_user_id = ?) OR "
            "(blocker_platform_user_id = ? AND blocked_platform_user_id = ?) LIMIT 1",
            (
                first_platform_user_id,
                second_platform_user_id,
                second_platform_user_id,
                first_platform_user_id,
            ),
        ).fetchone()
    return row is not None
