"""M5 独立真人会话/拉黑的 owner/visitor 锚定存储原语。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Dict, Iterator, List, Optional

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "create_human_conversation_for_visit",
    "get_human_conversation_for_participant",
    "get_human_conversation_for_visit",
    "get_human_message_for_participant",
    "get_human_message_by_client_id",
    "has_platform_user_block",
    "hide_human_conversation_for_participant",
    "insert_human_chat_report",
    "insert_human_message",
    "insert_platform_user_block",
    "list_human_conversations_for_participant",
    "list_human_messages_for_participant",
    "lock_human_conversation",
    "mark_human_conversation_read",
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
    include_hidden: bool = False,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 conversation + participant 读取；第三方返回 None。"""
    hidden = ""
    params: list[Any] = [conversation_id, platform_user_id, platform_user_id]
    if not include_hidden:
        hidden = (
            " AND ((owner_platform_user_id = ? AND owner_hidden_at IS NULL) "
            "OR (visitor_platform_user_id = ? AND visitor_hidden_at IS NULL))"
        )
        params.extend([platform_user_id, platform_user_id])
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE id = ? "
            "AND (owner_platform_user_id = ? OR visitor_platform_user_id = ?)"
            + hidden,
            tuple(params),
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
        " AND ((c.owner_platform_user_id = ? AND c.owner_hidden_at IS NULL) "
        "OR (c.visitor_platform_user_id = ? AND c.visitor_hidden_at IS NULL))"
    )
    params: list[Any] = [platform_user_id, platform_user_id]
    if not include_hidden:
        params.extend([platform_user_id, platform_user_id])
    params.append(max(1, min(int(limit), 100)))
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT c.*, owner.display_name AS owner_display_name, "
            "visitor.display_name AS visitor_display_name "
            "FROM human_conversations c "
            "JOIN platform_users owner ON owner.id = c.owner_platform_user_id "
            "JOIN platform_users visitor ON visitor.id = c.visitor_platform_user_id "
            "WHERE (c.owner_platform_user_id = ? OR c.visitor_platform_user_id = ?)"
            + hidden
            + " ORDER BY COALESCE(c.last_message_at, c.created_at) DESC, c.id DESC LIMIT ?",
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


def lock_human_conversation(
    *, conversation_id: str, conn: Connection
) -> Optional[Dict[str, Any]]:
    """在调用方事务内锁真人 conversation 行。"""
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = conn.execute(
        "SELECT * FROM human_conversations WHERE id = ?" + suffix,
        (conversation_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_human_message(
    *,
    conversation_id: str,
    sender_platform_user_id: str,
    client_message_id: str,
    body_text: str,
    created_at: str,
    conn: Connection,
) -> tuple[Dict[str, Any], bool]:
    """在已锁定 active conversation 中幂等插入真人消息。"""
    existing = conn.execute(
        "SELECT * FROM human_messages WHERE conversation_id = ? "
        "AND sender_platform_user_id = ? AND client_message_id = ?",
        (conversation_id, sender_platform_user_id, client_message_id),
    ).fetchone()
    if existing is not None:
        result = dict(existing)
        if result["body_text"] != body_text:
            raise ValueError("idempotency_conflict")
        return result, False
    message_id = _new_id("hmsg")
    sequence_row = conn.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
        "FROM human_messages WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    sequence_no = int(sequence_row["next_sequence"])
    conn.execute(
        "INSERT INTO human_messages(id, conversation_id, sender_platform_user_id, "
        "client_message_id, sequence_no, body_text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (
            message_id,
            conversation_id,
            sender_platform_user_id,
            client_message_id,
            sequence_no,
            body_text,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM human_messages WHERE conversation_id = ? "
        "AND sender_platform_user_id = ? AND client_message_id = ?",
        (conversation_id, sender_platform_user_id, client_message_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("human message was not inserted")
    result = dict(row)
    created = result["id"] == message_id
    if not created and result["body_text"] != body_text:
        raise ValueError("idempotency_conflict")
    if created:
        conn.execute(
            "UPDATE human_conversations SET last_message_at = ?, updated_at = ? "
            "WHERE id = ?",
            (created_at, created_at, conversation_id),
        )
    return result, created


def list_human_messages_for_participant(
    *,
    conversation_id: str,
    platform_user_id: str,
    cursor_sequence_no: Optional[int] = None,
    cursor_message_id: Optional[str] = None,
    limit: int = 21,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 participant + tuple cursor 读取未 self-hide 的真人历史。"""
    if (cursor_sequence_no is None) != (cursor_message_id is None):
        raise ValueError("invalid_cursor")
    params: list[Any] = [
        conversation_id,
        platform_user_id,
        platform_user_id,
        platform_user_id,
        platform_user_id,
    ]
    cursor_clause = ""
    if cursor_sequence_no is not None and cursor_message_id is not None:
        cursor_clause = "AND m.sequence_no < ?"
        params.append(int(cursor_sequence_no))
    params.append(max(1, min(int(limit), 51)))
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT m.* FROM human_messages m JOIN human_conversations c "
            "ON c.id = m.conversation_id WHERE c.id = ? "
            "AND (c.owner_platform_user_id = ? OR c.visitor_platform_user_id = ?) "
            "AND ((c.owner_platform_user_id = ? AND c.owner_hidden_at IS NULL) "
            "OR (c.visitor_platform_user_id = ? AND c.visitor_hidden_at IS NULL)) "
            + cursor_clause
            + " ORDER BY m.sequence_no DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def get_human_message_for_participant(
    *,
    conversation_id: str,
    message_id: str,
    platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """读取 participant 可见的单条真人消息，供举报 evidence snapshot。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT m.* FROM human_messages m JOIN human_conversations c "
            "ON c.id = m.conversation_id WHERE m.id = ? AND c.id = ? "
            "AND (c.owner_platform_user_id = ? OR c.visitor_platform_user_id = ?)",
            (message_id, conversation_id, platform_user_id, platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_human_message_by_client_id(
    *,
    conversation_id: str,
    sender_platform_user_id: str,
    client_message_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 sender-scoped client id 查幂等消息。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM human_messages WHERE conversation_id = ? "
            "AND sender_platform_user_id = ? AND client_message_id = ?",
            (conversation_id, sender_platform_user_id, client_message_id),
        ).fetchone()
    return dict(row) if row else None


def mark_human_conversation_read(
    *,
    conversation_id: str,
    platform_user_id: str,
    read_at: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """只更新当前 participant 的 read marker。"""
    with _human_write_tx(conn) as tx:
        conversation = lock_human_conversation(
            conversation_id=conversation_id, conn=tx
        )
        if conversation is None or platform_user_id not in {
            conversation["owner_platform_user_id"],
            conversation["visitor_platform_user_id"],
        }:
            return None
        column = (
            "owner_last_read_at"
            if conversation["owner_platform_user_id"] == platform_user_id
            else "visitor_last_read_at"
        )
        tx.execute(
            f"UPDATE human_conversations SET {column} = ?, updated_at = ? WHERE id = ?",
            (read_at, read_at, conversation_id),
        )
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    return dict(row) if row else None


def hide_human_conversation_for_participant(
    *,
    conversation_id: str,
    platform_user_id: str,
    hidden_at: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """只写当前 participant 的 hidden_at，不删除 conversation/message。"""
    with _human_write_tx(conn) as tx:
        conversation = lock_human_conversation(
            conversation_id=conversation_id, conn=tx
        )
        if conversation is None or platform_user_id not in {
            conversation["owner_platform_user_id"],
            conversation["visitor_platform_user_id"],
        }:
            return None
        column = (
            "owner_hidden_at"
            if conversation["owner_platform_user_id"] == platform_user_id
            else "visitor_hidden_at"
        )
        tx.execute(
            f"UPDATE human_conversations SET {column} = COALESCE({column}, ?), "
            "updated_at = ? WHERE id = ?",
            (hidden_at, hidden_at, conversation_id),
        )
        row = tx.execute(
            "SELECT * FROM human_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    return dict(row) if row else None


def insert_human_chat_report(
    *,
    conversation_id: str,
    reporter_platform_user_id: str,
    reported_platform_user_id: str,
    reported_message_id: Optional[str],
    reason_code: str,
    details_text: Optional[str],
    evidence_snapshot: Dict[str, Any],
    created_at: str,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """追加独立 immutable 举报证据；用户侧无 update/delete 原语。"""
    report_id = _new_id("hrep")
    evidence_json = json.dumps(
        evidence_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with _human_write_tx(conn) as tx:
        conversation = lock_human_conversation(
            conversation_id=conversation_id, conn=tx
        )
        if conversation is None or reporter_platform_user_id not in {
            conversation["owner_platform_user_id"],
            conversation["visitor_platform_user_id"],
        }:
            raise ValueError("human conversation not found")
        expected_reported = (
            conversation["visitor_platform_user_id"]
            if conversation["owner_platform_user_id"] == reporter_platform_user_id
            else conversation["owner_platform_user_id"]
        )
        if expected_reported != reported_platform_user_id:
            raise ValueError("reported participant mismatch")
        tx.execute(
            "INSERT INTO human_chat_reports(id, conversation_id, "
            "reporter_platform_user_id, reported_platform_user_id, reported_message_id, "
            "reason_code, details_text, evidence_snapshot_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                report_id,
                conversation_id,
                reporter_platform_user_id,
                reported_platform_user_id,
                reported_message_id,
                reason_code,
                details_text,
                evidence_json,
                created_at,
            ),
        )
        row = tx.execute(
            "SELECT * FROM human_chat_reports WHERE id = ?", (report_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("human chat report was not created")
    result = dict(row)
    result["evidence_snapshot"] = json.loads(result.pop("evidence_snapshot_json"))
    return result
