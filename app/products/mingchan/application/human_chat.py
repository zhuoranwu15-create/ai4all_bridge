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
from app.db._core import connect
from app.platform.media.moderation import media_moderation_ready
from app.platform.media.persistence import mark_media_assets_referenced
from app.platform.media.view import media_preview_text
from app.products.mingchan.domain.companion_world.human_chat import (
    HUMAN_REPORT_REASONS,
    HUMAN_REPORT_REASONS_VERSION,
    human_report_reason,
    normalize_human_message_body,
)
from app.products.mingchan.application.visits import CompanionWorldVisitService
from app.time_utils import parse_db_timestamp





class HumanChatError(Exception):
    """M5 human chat 的稳定业务错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _db_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _write_begin(conn) -> None:
    """Compatibility hook; psycopg starts the transaction on the first statement."""


def _read_only_reason(row: Dict[str, Any], *, write_enabled: bool) -> Optional[str]:
    """会话为何只读；可发送时返回 ``None``。

    先判终态再判开关：visit 结束是不可逆的，而 ``feature_disabled`` 是可恢复的运营态。
    反过来排序会让一个已经结束的会话显示成「功能未开放」，等开关打开又变「已结束」。
    """
    visit_status = row.get("visit_status")
    if visit_status == "blocked":
        return "counterpart_blocked"
    if visit_status != "active":
        # 含 expired/left/revoked/rejected/cancelled/pending，以及 visit 行已不存在。
        return "visit_ended"
    if row.get("status") != "active":
        return "conversation_ended"
    if not write_enabled:
        return "feature_disabled"
    return None


def _preview_text(value: Optional[str], media_kind: Optional[str] = None) -> Optional[str]:
    """列表预览：折叠换行与连续空白，让单行渲染不被正文排版撑破。

    最后一条是媒体消息时正文常为空，落 ``[图片]``/``[语音]`` 占位而不是空白气泡。
    """
    if value is None and not media_kind:
        return None
    if media_kind:
        return media_preview_text(caption=value, kind=media_kind) or None
    collapsed = " ".join(str(value).split())
    return collapsed or None


def _conversation_public(
    row: Dict[str, Any], platform_user_id: str, *, write_enabled: bool = False
) -> Dict[str, Any]:
    is_owner = row["owner_platform_user_id"] == platform_user_id
    reason = _read_only_reason(row, write_enabled=write_enabled)
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
        "last_preview": _preview_text(
            row.get("last_preview"), row.get("last_media_kind")
        ),
        "unread_count": int(row.get("unread_count") or 0),
        "can_send": reason is None,
        "read_only_reason": reason,
        "expires_at": row.get("visit_expires_at"),
    }


class CompanionWorldHumanChatService:
    """一对一真人文字聊天；所有 sender/participant 均来自 session。"""

    def list_conversations(
        self, platform_user_id: str, *, now: datetime, write_enabled: bool = False
    ) -> Sequence[Dict[str, Any]]:
        """request-time 处理 visit expiry 后返回未 self-hide 的会话。

        先跑 expiry 再读列表，因此 ``visit_status`` 一定是最新的，``can_send`` 不需要
        再拿 ``now`` 和 ``expires_at`` 二次比较。
        """
        CompanionWorldVisitService().list_visits_current(
            platform_user_id, now=now
        )
        rows = list_human_conversations_for_participant(
            platform_user_id=platform_user_id, include_hidden=False, limit=100
        )
        return tuple(
            _conversation_public(row, platform_user_id, write_enabled=write_enabled)
            for row in rows
        )

    def list_messages(
        self,
        platform_user_id: str,
        *,
        conversation_id: str,
        now: datetime,
        cursor_sequence_no: Optional[int],
        cursor_message_id: Optional[str],
        limit: int,
    ) -> tuple[Dict[str, Any], Sequence[Dict[str, Any]]]:
        """active/read_only 均可读取；self-hidden 与第三方统一 not found。

        返回 ``(conversation, rows)``：媒体消息要按 ``visit`` 维度签读 URL（对方发来的图不
        属于自己，只能用 ``visit:<id>`` scope），调用方需要会话行上的 visit_id 与到期时间。
        """
        conversation = get_human_conversation_for_participant(
            conversation_id=conversation_id, platform_user_id=platform_user_id
        )
        if conversation is None:
            raise HumanChatError("human_conversation_not_found")
        CompanionWorldVisitService().list_visits_current(
            platform_user_id, now=now
        )
        # visit 到期时间不在 human_conversations 上，但访客 TTL 要取 min(配置, visit 剩余)；
        # 放在 expiry 处理之后读，拿到的一定是最新状态。
        visit = get_universe_visit(visit_id=conversation["visit_id"])
        conversation = dict(conversation)
        conversation["visit_expires_at"] = (visit or {}).get("expires_at")
        try:
            rows = tuple(
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
        return conversation, rows

    def send(
        self,
        platform_user_id: str,
        *,
        conversation_id: str,
        client_message_id: str,
        body_text: str,
        now: datetime,
        write_enabled: bool,
        media_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """在 visitor→world→visit→conversation 锁序下原子发送真人文字或媒体。

        ``media_id`` 由调用方在 API 层完成 owner 锚定与门控校验；这里只负责在**同一事务**
        内把资产从 ``pending`` 翻成 ``referenced``，保证「消息已存但媒体已被回收」不可能发生。
        """
        try:
            # 媒体消息允许空 caption（图片不带文字是常态），长度上限仍然生效。
            clean_body = normalize_human_message_body(
                body_text, allow_empty=media_id is not None
            )
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
                # 同一个 client_message_id 换正文或换图都算冲突，不允许悄悄改写已发出的消息。
                if (
                    replay["body_text"] != clean_body
                    or replay.get("media_id") != media_id
                ):
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
                if media_id:
                    # 与消息插入同一事务：认领失败整条消息一起回滚，不留 referenced 孤儿。
                    try:
                        mark_media_assets_referenced(
                            media_ids=[media_id],
                            owner_platform_user_id=platform_user_id,
                            conn=conn,
                            # 机审整链可跑才入队（谓词与批处理同一个，见 media_moderation_ready）；
                            # 会话图命中红线只记录不撤回（D-7 已知敞口），但审核结论仍要落库，
                            # 供事后人工处置与 v1.6 撤回补做。
                            queue_moderation=media_moderation_ready(),
                        )
                    except ValueError as err:
                        raise HumanChatError("media_ref_invalid") from err
                try:
                    message, created = insert_human_message(
                        conversation_id=conversation_id,
                        sender_platform_user_id=platform_user_id,
                        client_message_id=client_message_id,
                        body_text=clean_body,
                        media_id=media_id,
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

    def report_options(self) -> Dict[str, Any]:
        """返回版本化举报原因表；与 :meth:`report` 的校验共用同一份领域受控表。"""
        return {
            "version": HUMAN_REPORT_REASONS_VERSION,
            "options": [
                {
                    "reason_code": reason.reason_code,
                    "label": reason.label,
                    "details_required": reason.details_required,
                }
                for reason in HUMAN_REPORT_REASONS
            ],
        }

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
        reason = human_report_reason(reason_code)
        if reason is None:
            raise HumanChatError("invalid_request")
        details = str(details_text or "").strip() or None
        if details and len(details) > 1000:
            raise HumanChatError("invalid_request")
        # 契约里 details_required 为真的码必须真的强制，否则 report-options 在撒谎。
        if reason.details_required and details is None:
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
            reason_code=reason.reason_code,
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
