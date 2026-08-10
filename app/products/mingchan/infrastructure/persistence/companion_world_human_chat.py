"""M5 独立真人会话/拉黑的 owner/visitor 锚定存储原语。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Dict, Iterator, List, Optional

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._backend import Connection
from app.db._core import _new_id, _tx, connect

#: 列表页预览截断长度。正文上限 4000 字，一页最多 100 条，不截断会让列表响应到 400KB。
HUMAN_CONVERSATION_PREVIEW_CHARS = 120

__all__ = [
    "HUMAN_CONVERSATION_PREVIEW_CHARS",
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
    """复用调用方事务，或建立独立 PostgreSQL 写事务。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
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
    suffix = " FOR UPDATE"
    with _human_write_tx(conn) as tx:
        visit = tx.execute(
            "SELECT v.id, v.owner_platform_user_id, v.visitor_platform_user_id, v.status "
            "FROM universe_visits v JOIN universes u ON u.id = v.universe_id "
            "WHERE v.id = ? AND u.app_id = ?" + suffix,
            (visit_id, MINGCHAN_APP_ID),
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
    params: list[Any] = [
        conversation_id,
        platform_user_id,
        platform_user_id,
        MINGCHAN_APP_ID,
    ]
    if not include_hidden:
        hidden = (
            " AND ((c.owner_platform_user_id = ? AND c.owner_hidden_at IS NULL) "
            "OR (c.visitor_platform_user_id = ? AND c.visitor_hidden_at IS NULL))"
        )
        params.extend([platform_user_id, platform_user_id])
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT c.* FROM human_conversations c "
            "JOIN universe_visits v ON v.id = c.visit_id "
            "JOIN universes u ON u.id = v.universe_id WHERE c.id = ? "
            "AND (c.owner_platform_user_id = ? OR c.visitor_platform_user_id = ?) "
            "AND u.app_id = ?"
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
            "SELECT c.* FROM human_conversations c "
            "JOIN universe_visits v ON v.id = c.visit_id "
            "JOIN universes u ON u.id = v.universe_id "
            "WHERE c.visit_id = ? AND u.app_id = ?",
            (visit_id, MINGCHAN_APP_ID),
        ).fetchone()
    return dict(row) if row else None


def list_human_conversations_for_participant(
    *,
    platform_user_id: str,
    include_hidden: bool = False,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出 participant 会话；默认按各自 hidden 字段过滤。

    未读数、最近预览与 visit 到期时间都在同一条 SQL 里用相关子查询取回（M5-CONV-001），
    不逐会话补查——一页最多 100 行，逐行查就是 300 次往返。预览在 SQL 里就截断，避免把
    100 条 4000 字正文全量搬回进程再丢掉。
    """
    hidden = "" if include_hidden else (
        " AND ((c.owner_platform_user_id = ? AND c.owner_hidden_at IS NULL) "
        "OR (c.visitor_platform_user_id = ? AND c.visitor_hidden_at IS NULL))"
    )
    # 参数顺序必须与 SQL 中 ? 的出现顺序严格一致；这里的值恰好全是同一个 id，写错不会
    # 报错只会算错，所以逐个占位符列出而不是循环生成。
    params: list[Any] = [
        HUMAN_CONVERSATION_PREVIEW_CHARS,  # SUBSTR 截断长度
        platform_user_id,                  # 未读子查询：sender <> 自己
        platform_user_id,                  # 未读子查询：CASE 判定自己是不是 owner
        platform_user_id,                  # WHERE owner = 自己
        platform_user_id,                  # WHERE visitor = 自己
        MINGCHAN_APP_ID,                   # Mingchan visit/world scope
    ]
    if not include_hidden:
        params.extend([platform_user_id, platform_user_id])
    params.append(max(1, min(int(limit), 100)))
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT c.*, owner.display_name AS owner_display_name, "
            "visitor.display_name AS visitor_display_name, "
            "v.status AS visit_status, v.expires_at AS visit_expires_at, "
            "(SELECT SUBSTR(m.body_text, 1, ?) FROM human_messages m "
            " WHERE m.conversation_id = c.id "
            " ORDER BY m.sequence_no DESC LIMIT 1) AS last_preview, "
            # 最后一条若是媒体消息，正文可能为空；带上 kind 让上层落 [图片]/[语音] 占位。
            # 必须用 LEFT JOIN：INNER JOIN 会跳过最新的纯文本消息，把更早的图片 kind 顶上来。
            "(SELECT a.kind FROM human_messages m "
            " LEFT JOIN media_assets a ON a.id = m.media_id "
            " WHERE m.conversation_id = c.id "
            " ORDER BY m.sequence_no DESC LIMIT 1) AS last_media_kind, "
            # 未读只数对方发的；游标为 NULL 等价于一条都没读过。
            "(SELECT COUNT(*) FROM human_messages m "
            " WHERE m.conversation_id = c.id "
            "   AND m.sender_platform_user_id <> ? "
            "   AND m.sequence_no > COALESCE(CASE WHEN c.owner_platform_user_id = ? "
            "       THEN c.owner_last_read_sequence ELSE c.visitor_last_read_sequence "
            "       END, 0)) AS unread_count "
            "FROM human_conversations c "
            "JOIN platform_users owner ON owner.id = c.owner_platform_user_id "
            "JOIN platform_users visitor ON visitor.id = c.visitor_platform_user_id "
            "LEFT JOIN universe_visits v ON v.id = c.visit_id "
            "JOIN universes u ON u.id = v.universe_id "
            "WHERE (c.owner_platform_user_id = ? OR c.visitor_platform_user_id = ?)"
            " AND u.app_id = ?"
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
    suffix = " FOR UPDATE"
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
    media_id: Optional[str] = None,
) -> tuple[Dict[str, Any], bool]:
    """在已锁定 active conversation 中幂等插入真人消息。

    ``media_id`` 是 v1.5 媒体消息的资产引用；重放时它与 ``body_text`` 一起参与幂等比对——
    同一个 ``client_message_id`` 换一张图必须报冲突，否则客户端能悄悄改写已发出的消息。
    """
    existing = conn.execute(
        "SELECT * FROM human_messages WHERE conversation_id = ? "
        "AND sender_platform_user_id = ? AND client_message_id = ?",
        (conversation_id, sender_platform_user_id, client_message_id),
    ).fetchone()
    if existing is not None:
        result = dict(existing)
        if result["body_text"] != body_text or result.get("media_id") != media_id:
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
        "client_message_id, sequence_no, body_text, media_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (
            message_id,
            conversation_id,
            sender_platform_user_id,
            client_message_id,
            sequence_no,
            body_text,
            media_id,
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
    if not created and (
        result["body_text"] != body_text or result.get("media_id") != media_id
    ):
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
    """只更新当前 participant 的 read marker。

    时间戳与序号游标一起推进：``last_read_at`` 供展示，``last_read_sequence`` 才是未读数
    的判据（m0052）。序号在**同一个写事务内**快照当前最大 ``sequence_no``，因此不存在
    「读到一半又进来一条被顺手标已读」的窗口。游标只前进不回退。
    """
    with _human_write_tx(conn) as tx:
        conversation = lock_human_conversation(
            conversation_id=conversation_id, conn=tx
        )
        if conversation is None or platform_user_id not in {
            conversation["owner_platform_user_id"],
            conversation["visitor_platform_user_id"],
        }:
            return None
        is_owner = conversation["owner_platform_user_id"] == platform_user_id
        read_at_column = "owner_last_read_at" if is_owner else "visitor_last_read_at"
        sequence_column = (
            "owner_last_read_sequence" if is_owner else "visitor_last_read_sequence"
        )
        latest = tx.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) AS seq FROM human_messages "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        target = int(latest["seq"] or 0)
        tx.execute(
            f"UPDATE human_conversations SET {read_at_column} = ?, "
            f"{sequence_column} = CASE WHEN {sequence_column} IS NULL "
            f"  OR {sequence_column} < ? THEN ? ELSE {sequence_column} END, "
            "updated_at = ? WHERE id = ?",
            (read_at, target, target, read_at, conversation_id),
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
