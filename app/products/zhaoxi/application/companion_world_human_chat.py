"""M5 独立真人聊天 service：participant ACL、发送事务、self-hide 与举报。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Sequence

from app.db import (
    get_human_conversation_for_participant,
    get_human_message_for_participant,
    get_human_message_by_client_id,
    get_universe_visit,
    has_platform_user_block,
    hide_human_conversation_for_participant,
    insert_human_chat_report,
    insert_human_message,
    list_human_conversations_for_participant,
    list_human_messages_for_participant,
    lock_human_conversation,
    lock_platform_user_for_visit,
    lock_universe,
    lock_universe_visit,
    mark_human_conversation_read,
    mark_universe_visit_terminal,
)
from app.db._backend import is_postgres
from app.db._core import connect
from app.products.zhaoxi.domain.companion_world.human_chat import normalize_human_message_body
from app.products.zhaoxi.application.companion_world_visits import CompanionWorldVisitService
from app.time_utils import parse_db_timestamp

_REPORT_REASONS = {
    "spam",
    "harassment",
    "threat",
    "hate",
    "sexual",
    "privacy",
    "other",
}


class HumanChatError(Exception):
    """M5 human chat 的稳定业务错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _db_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _write_begin(conn) -> None:
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")


def _conversation_public(row: Dict[str, Any], platform_user_id: str) -> Dict[str, Any]:
    is_owner = row["owner_platform_user_id"] == platform_user_id
    return {
        "conversation_id": row["id"],
        "visit_id": row["visit_id"],
        "counterpart_display_name": row.get(
            "visitor_display_name" if is_owner else "owner_display_name"
        ),
        "status": row["status"],
        "last_message_at": row.get("last_message_at"),
        "last_read_at": row.get(
            "owner_last_read_at" if is_owner else "visitor_last_read_at"
        ),
        "created_at": row["created_at"],
    }


class CompanionWorldHumanChatService:
    """一对一真人文字聊天；所有 sender/participant 均来自 session。"""

    def list_conversations(
        self, platform_user_id: str, *, now: datetime
    ) -> Sequence[Dict[str, Any]]:
        """request-time 处理 visit expiry 后返回未 self-hide 的会话。"""
        CompanionWorldVisitService().list_visits_current(
            platform_user_id, now=now
        )
        rows = list_human_conversations_for_participant(
            platform_user_id=platform_user_id, include_hidden=False, limit=100
        )
        return tuple(_conversation_public(row, platform_user_id) for row in rows)

    def list_messages(
        self,
        platform_user_id: str,
        *,
        conversation_id: str,
        now: datetime,
        cursor_sequence_no: Optional[int],
        cursor_message_id: Optional[str],
        limit: int,
    ) -> Sequence[Dict[str, Any]]:
        """active/read_only 均可读取；self-hidden 与第三方统一 not found。"""
        conversation = get_human_conversation_for_participant(
            conversation_id=conversation_id, platform_user_id=platform_user_id
        )
        if conversation is None:
            raise HumanChatError("human_conversation_not_found")
        CompanionWorldVisitService().list_visits_current(
            platform_user_id, now=now
        )
        try:
            return tuple(
                list_human_messages_for_participant(
                    conversation_id=conversation_id,
                    platform_user_id=platform_user_id,
                    cursor_sequence_no=cursor_sequence_no,
                    cursor_message_id=cursor_message_id,
                    limit=limit,
                )
            )
        except ValueError as err:
            raise HumanChatError("invalid_cursor") from err

    def send(
        self,
        platform_user_id: str,
        *,
        conversation_id: str,
        client_message_id: str,
        body_text: str,
        now: datetime,
        write_enabled: bool,
    ) -> Dict[str, Any]:
        """在 visitor→world→visit→conversation 锁序下原子发送真人文字。"""
        try:
            clean_body = normalize_human_message_body(body_text)
        except ValueError as err:
            raise HumanChatError("invalid_request") from err
        preview = get_human_conversation_for_participant(
            conversation_id=conversation_id, platform_user_id=platform_user_id
        )
        if preview is None:
            raise HumanChatError("human_conversation_not_found")
        visit = get_universe_visit(visit_id=preview["visit_id"])
        if visit is None:
            raise HumanChatError("human_chat_read_only")
        expired = False
        blocked = False
        message: Optional[Dict[str, Any]] = None
        created = False
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(
                platform_user_id=visit["visitor_platform_user_id"], conn=conn
            )
            lock_universe(universe_id=visit["universe_id"], conn=conn)
            locked_visit = lock_universe_visit(visit_id=visit["id"], conn=conn)
            locked_conversation = lock_human_conversation(
                conversation_id=conversation_id, conn=conn
            )
            if (
                locked_visit is None
                or locked_conversation is None
                or platform_user_id
                not in {
                    locked_conversation["owner_platform_user_id"],
                    locked_conversation["visitor_platform_user_id"],
                }
            ):
                raise HumanChatError("human_conversation_not_found")
            hidden_field = (
                "owner_hidden_at"
                if locked_conversation["owner_platform_user_id"] == platform_user_id
                else "visitor_hidden_at"
            )
            if locked_conversation[hidden_field] is not None:
                raise HumanChatError("human_conversation_not_found")
            replay = get_human_message_by_client_id(
                conversation_id=conversation_id,
                sender_platform_user_id=platform_user_id,
                client_message_id=client_message_id,
                conn=conn,
            )
            if replay is not None:
                if replay["body_text"] != clean_body:
                    raise HumanChatError("idempotency_conflict")
                message = replay
                created = False
            elif not write_enabled:
                raise HumanChatError("human_chat_read_only")
            elif (
                locked_visit["status"] != "active"
                or locked_conversation["status"] != "active"
            ):
                raise HumanChatError("human_chat_read_only")
            elif now >= parse_db_timestamp(locked_visit["expires_at"]):
                mark_universe_visit_terminal(
                    visit_id=locked_visit["id"],
                    expected_status="active",
                    target_status="expired",
                    now=_db_time(now),
                    terminal_reason="active_expired",
                    conn=conn,
                )
                expired = True
            elif has_platform_user_block(
                first_platform_user_id=locked_visit["owner_platform_user_id"],
                second_platform_user_id=locked_visit["visitor_platform_user_id"],
                conn=conn,
            ):
                mark_universe_visit_terminal(
                    visit_id=locked_visit["id"],
                    expected_status="active",
                    target_status="blocked",
                    now=_db_time(now),
                    terminal_reason="contact_blocked",
                    conn=conn,
                )
                blocked = True
            else:
                try:
                    message, created = insert_human_message(
                        conversation_id=conversation_id,
                        sender_platform_user_id=platform_user_id,
                        client_message_id=client_message_id,
                        body_text=clean_body,
                        created_at=_db_time(now),
                        conn=conn,
                    )
                except ValueError as err:
                    if str(err) == "idempotency_conflict":
                        raise HumanChatError("idempotency_conflict") from err
                    raise
        if expired or blocked:
            raise HumanChatError("human_chat_read_only")
        if message is None:
            raise RuntimeError("human message missing after send")
        return {"message": message, "created": created}

    def mark_read(
        self, platform_user_id: str, *, conversation_id: str, now: datetime
    ) -> Dict[str, Any]:
        """更新当前 participant read marker。"""
        if get_human_conversation_for_participant(
            conversation_id=conversation_id, platform_user_id=platform_user_id
        ) is None:
            raise HumanChatError("human_conversation_not_found")
        row = mark_human_conversation_read(
            conversation_id=conversation_id,
            platform_user_id=platform_user_id,
            read_at=_db_time(now),
        )
        if row is None:
            raise HumanChatError("human_conversation_not_found")
        return row

    def hide(
        self, platform_user_id: str, *, conversation_id: str, now: datetime
    ) -> Dict[str, Any]:
        """只隐藏当前 participant entry/history，不物理删除任何 message。"""
        row = hide_human_conversation_for_participant(
            conversation_id=conversation_id,
            platform_user_id=platform_user_id,
            hidden_at=_db_time(now),
        )
        if row is None:
            raise HumanChatError("human_conversation_not_found")
        return row

    def report(
        self,
        platform_user_id: str,
        *,
        conversation_id: str,
        message_id: Optional[str],
        reason_code: str,
        details_text: Optional[str],
        block_after: bool,
        now: datetime,
    ) -> Dict[str, Any]:
        """复制必要 evidence snapshot；可在报告提交后立即 block。"""
        conversation = get_human_conversation_for_participant(
            conversation_id=conversation_id, platform_user_id=platform_user_id
        )
        if conversation is None:
            raise HumanChatError("human_conversation_not_found")
        reason = str(reason_code or "").strip().lower()
        if reason not in _REPORT_REASONS:
            raise HumanChatError("invalid_request")
        details = str(details_text or "").strip() or None
        if details and len(details) > 1000:
            raise HumanChatError("invalid_request")
        counterpart = (
            conversation["visitor_platform_user_id"]
            if conversation["owner_platform_user_id"] == platform_user_id
            else conversation["owner_platform_user_id"]
        )
        reported_message = None
        if message_id:
            reported_message = get_human_message_for_participant(
                conversation_id=conversation_id,
                message_id=message_id,
                platform_user_id=platform_user_id,
            )
            if (
                reported_message is None
                or reported_message["sender_platform_user_id"] != counterpart
            ):
                raise HumanChatError("human_message_not_found")
        snapshot: Dict[str, Any] = {
            "version": 1,
            "captured_at": _db_time(now),
            "conversation_id": conversation_id,
            "reported_message": None,
        }
        if reported_message is not None:
            snapshot["reported_message"] = {
                "message_id": reported_message["id"],
                "body_text": reported_message["body_text"],
                "created_at": reported_message["created_at"],
            }
        report = insert_human_chat_report(
            conversation_id=conversation_id,
            reporter_platform_user_id=platform_user_id,
            reported_platform_user_id=counterpart,
            reported_message_id=message_id,
            reason_code=reason,
            details_text=details,
            evidence_snapshot=snapshot,
            created_at=_db_time(now),
        )
        block_result = None
        if block_after:
            block_result = CompanionWorldVisitService().block(
                platform_user_id, visit_id=conversation["visit_id"], now=now
            )
        return {"report": report, "block": block_result}

    def block(
        self, platform_user_id: str, *, conversation_id: str, now: datetime
    ) -> Dict[str, Any]:
        """从真人会话入口复用 visit contact block 强事务。"""
        conversation = get_human_conversation_for_participant(
            conversation_id=conversation_id,
            platform_user_id=platform_user_id,
            include_hidden=True,
        )
        if conversation is None:
            raise HumanChatError("human_conversation_not_found")
        return CompanionWorldVisitService().block(
            platform_user_id, visit_id=conversation["visit_id"], now=now
        )


__all__ = ["CompanionWorldHumanChatService", "HumanChatError"]
