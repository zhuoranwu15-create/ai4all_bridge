"""app.db.accounts — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import Connection, IntegrityError, Row, is_postgres
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from app.bootstrap.product_registry import (
    PRODUCTION_PRODUCT_REGISTRY,
    ZHAOXI_APP_ID,
    ProductRegistry,
)
from app.config import settings
from app.db._core import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    APP_ACTIVE_SESSION_KEY,
    MODERATION_BLOCKED_ERROR,
    _NON_CONTEXT_ASSISTANT_REPLY,
    _UNSET,
    _channel_account_id_aliases,
    _clean_text,
    _new_id,
    _normalize_phone,
    _savepoint,
    _tx,
    advisory_lock_key,
    connect,
    logger,
    product_quota_subject,
)
from app.db.product_memberships import _require_active_product_membership_in_conn
__all__ = [
    'SessionPrincipal',
    'resolve_owner_platform_user_id',
    'clear_all_messages_for_account',
    'clear_session_messages',
    'consume_valid_verification_token',
    'consume_verification_token',
    'count_context_messages_for_session',
    'list_context_messages_for_session',
    'update_session_rolling_summary',
    'count_reactivation_outbound_for_quota_date',
    'count_inbound_messages_for_account',
    'count_recent_inbound_messages_for_account',
    'count_verifications_last_hour',
    'create_phone_verification',
    'create_platform_user_session',
    'get_account',
    'get_account_product_access',
    'get_account_id_for_session_key',
    'get_account_last_inbound_at',
    'get_account_onboarding_state',
    'get_daily_usage',
    'get_duplicate_reply',
    'get_duplicate_reply_record',
    'get_first_user_message_at',
    'get_latest_active_verification',
    'get_latest_closed_carryover_for_account',
    'get_latest_message_id_for_account',
    'get_message_raw',
    'resolve_session_principal',
    'get_profile_for_account',
    'get_profile_for_session',
    'get_session',
    'get_session_for_account_and_key',
    'get_usage_last_7_days',
    'resolve_effective_quota_limits',
    'get_valid_verification_by_token',
    'get_verification_by_token',
    'increment_daily_usage',
    'reserve_daily_quota',
    'confirm_daily_quota',
    'rollback_daily_quota',
    'reclaim_expired_reservations',
    'increment_verify_attempts',
    'insert_message',
    'insert_outbound_delivery_message',
    'invalidate_other_verifications_for_phone',
    'invalidate_verification',
    'invalidate_verifications_for_phone',
    'list_accounts',
    'list_channel_bindings_for_account',
    'list_reactivation_outbound_messages_admin',
    'list_recent_message_raw',
    'list_recent_messages',
    'list_recent_context_messages_for_session',
    'list_recent_messages_for_account',
    'list_recent_messages_for_account_since',
    'list_recent_reactivation_outbound_messages',
    'list_session_messages',
    'list_session_messages_before',
    'list_app_conversation_messages_before',
    'summarize_app_conversations',
    'list_sessions',
    'list_sessions_for_account',
    'list_user_active_dates',
    'mark_message_moderation_blocked',
    'normalize_phone',
    'record_analytics_event',
    'revoke_platform_user_session',
    'set_account_debug_flag',
    'set_account_onboarding_state',
    'set_account_status',
    'set_verification_verified',
    'update_account',
    'update_profile_for_account',
    'update_profile_for_session',
    'upsert_channel_binding',
]
# ---------------------------------------------------------------------------
# Session / account creation
# ---------------------------------------------------------------------------

def upsert_channel_binding(
    *,
    account_id: str,
    channel: str,
    session_key: str,
    channel_account_id: Optional[str],
    sender_id: Optional[str],
    chat_id: Optional[str],
    raw_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_bindings(
                account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id, channel, session_key) DO UPDATE SET
                channel_account_id = COALESCE(excluded.channel_account_id, channel_bindings.channel_account_id),
                sender_id = COALESCE(excluded.sender_id, channel_bindings.sender_id),
                chat_id = COALESCE(excluded.chat_id, channel_bindings.chat_id),
                raw_identity_json = excluded.raw_identity_json,
                last_seen_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                account_id,
                channel,
                session_key,
                channel_account_id,
                sender_id,
                chat_id,
                json.dumps(raw_identity or {}, ensure_ascii=False),
            ),
        )
        # Deduplicate: remove stale rows for the same channel_account_id (different session_key).
        # This prevents two rows accumulating when QR completion and first inbound message
        # use different session_keys but refer to the same channel_account_id.
        if channel_account_id:
            aliases = _channel_account_id_aliases(channel_account_id)
            placeholders = ", ".join("?" for _ in aliases)
            conn.execute(
                f"""
                DELETE FROM channel_bindings
                WHERE account_id = ? AND channel = ?
                  AND channel_account_id IN ({placeholders})
                  AND session_key != ?
                """,
                (account_id, channel, *aliases, session_key),
            )
        row = conn.execute(
            """
            SELECT
                id, account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, first_seen_at, last_seen_at
            FROM channel_bindings
            WHERE account_id = ? AND channel = ? AND session_key = ?
            """,
            (account_id, channel, session_key),
        ).fetchone()
    item = dict(row)
    try:
        item["raw_identity"] = json.loads(item.pop("raw_identity_json") or "{}")
    except json.JSONDecodeError:
        item["raw_identity"] = {}
        item["raw_identity_decode_error"] = True
    return item


def list_channel_bindings_for_account(*, account_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, account_id, channel, session_key, channel_account_id,
                sender_id, chat_id, raw_identity_json, first_seen_at, last_seen_at
            FROM channel_bindings
            WHERE account_id = ?
            ORDER BY last_seen_at DESC, id DESC
            """,
            (account_id,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["raw_identity"] = json.loads(item.pop("raw_identity_json") or "{}")
        except json.JSONDecodeError:
            item["raw_identity"] = {}
            item["raw_identity_decode_error"] = True
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def insert_message(
    *,
    account_id: str,
    session_id: int,
    message_id: Optional[str],
    reply_to_message_id: Optional[str],
    direction: str,
    role: str,
    message_type: str,
    content: str,
    raw: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[int]:
    try:
        with _tx(conn) as tx:
            # _savepoint 保证 IntegrityError 只回滚到保存点，不污染外部事务（PG 下必须）。
            with _savepoint(tx, "insert_msg"):
                cursor = tx.execute(
                    """
                    INSERT INTO messages(
                        account_id, session_id, message_id, reply_to_message_id,
                        direction, role, message_type, content, raw_json, latency_ms, error,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                    """,
                    (
                        account_id,
                        session_id,
                        message_id,
                        reply_to_message_id,
                        direction,
                        role,
                        message_type,
                        content,
                        json.dumps(raw or {}, ensure_ascii=False),
                        latency_ms,
                        error,
                    ),
                )
                return int(cursor.lastrowid)
    except IntegrityError:
        return None


def mark_message_moderation_blocked(
    *,
    message_db_id: int,
    account_id: str,
    conn: Optional[Connection] = None,
) -> bool:
    """将入站命中消息标记为审核拦截，使其从所有 LLM 上下文/记忆路径中被过滤。

    复用 messages.error 列写入 MODERATION_BLOCKED_ERROR 标记；按 account_id 约束，
    原文仍保留在 messages.content 供管理端排查，审核任务另有独立 snapshot。
    """
    with _tx(conn) as tx:
        cursor = tx.execute(
            """
            UPDATE messages
            SET error = ?
            WHERE id = ? AND account_id = ?
            """,
            (MODERATION_BLOCKED_ERROR, int(message_db_id), account_id),
        )
        return cursor.rowcount > 0


def insert_outbound_delivery_message(
    *,
    outbound_message: Dict[str, Any],
    business_day: Optional[str] = None,
) -> Optional[int]:
    """Persist a delivered proactive outbound text into the account conversation timeline.

    ``business_day`` 由调用方（service 层）按与 turn_service 相同的口径算好后注入；
    仅在目标 session 尚无 business_day 时生效（get_or_create_session 内 COALESCE 写一次）。
    缺省 None 时保持旧行为（session business_day 留空）——但主动路径若不传，会造成 session
    因 business_day 为空而永不轮转/不做每日 dreaming，故 record_outbound_message_sent 恒传值。
    """
    from app.db.billing import get_or_create_session
    account_id = _clean_text(outbound_message.get("account_id"))
    channel = _clean_text(outbound_message.get("channel"))
    to_user_id = _clean_text(outbound_message.get("to_user_id"))
    text = _clean_text(outbound_message.get("text"))
    if not account_id or not channel or not to_user_id or not text:
        return None

    session_state = get_or_create_session(
        account_id=account_id,
        channel=channel,
        sender_id=to_user_id,
        sender_name=None,
        chat_id=to_user_id,
        session_key=ACCOUNT_ACTIVE_SESSION_KEY,
        business_day=_clean_text(business_day) or None,
        metadata={"created_reason": "proactive_outbound"},
    )
    session = session_state["session"]
    outbound_id = int(outbound_message["id"])
    raw_metadata = {
        "source": "proactive_outbound",
        "outbound_message_id": outbound_id,
        "gateway_message_id": outbound_message.get("gateway_message_id"),
        "idempotency_key": outbound_message.get("idempotency_key"),
        "channel": channel,
        "channel_account_id": outbound_message.get("channel_account_id"),
        "session_key": outbound_message.get("session_key"),
        "product_category": outbound_message.get("product_category"),
        "policy_version": outbound_message.get("policy_version"),
        "policy_reason": outbound_message.get("policy_reason"),
        "outbound_source": outbound_message.get("source"),
        "metadata": outbound_message.get("metadata") or {},
    }
    return insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"outbound-{outbound_id}",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content=text,
        raw=raw_metadata,
        error=outbound_message.get("error"),
    )


def get_duplicate_reply_record(
    *, account_id: str, reply_to_message_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """幂等命中时返回原持久化回复行（``content`` + ``message_id``）。

    App 链路重放必须回放**同一条** ``message_id``，否则客户端会为同一次发送渲染出第二个气泡
    （TURN-001）。``message_id`` 就是这行 outbound 消息落库时的 ``reply_message_id``。
    """
    if not reply_to_message_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT content, message_id FROM messages
            WHERE account_id = ?
              AND reply_to_message_id = ?
              AND direction = 'outbound'
            ORDER BY id DESC
            LIMIT 1
            """,
            (account_id, reply_to_message_id),
        ).fetchone()
        return dict(row) if row else None


def get_duplicate_reply(*, account_id: str, reply_to_message_id: Optional[str]) -> Optional[str]:
    """幂等命中的回复正文；只要正文的调用方（微信 turn 链路）继续用这个薄封装。"""
    record = get_duplicate_reply_record(
        account_id=account_id, reply_to_message_id=reply_to_message_id
    )
    return str(record["content"]) if record else None


def list_recent_messages(*, session_id: int, limit: int) -> List[Dict[str, str]]:
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE session_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def list_recent_messages_for_account(*, account_id: str, limit: int) -> List[Dict[str, Any]]:
    """Return recent context messages across sessions for one isolated account."""
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, message_id, role, content, created_at FROM messages
            WHERE account_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            # created_at 供组装期给历史 user 轮注入绝对时间戳（见 turn_service L0 时间戳）。
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def get_first_user_message_at(*, account_id: str) -> Optional[str]:
    """Return the timestamp ('YYYY-MM-DD HH:MM:SS') of the account's first inbound
    (user) message, or None if the account has never sent one. Account-scoped."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT MIN(created_at) AS first_at FROM messages
            WHERE account_id = ? AND role = 'user'
            """,
            (account_id,),
        ).fetchone()
    return row["first_at"] if row and row["first_at"] else None


def list_user_active_dates(*, account_id: str) -> List[str]:
    """Return the distinct dates ('YYYY-MM-DD', ascending) on which this account's
    user sent at least one message. Used to compute the chat streak. Account-scoped."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(created_at, 1, 10) AS d FROM messages
            WHERE account_id = ? AND role = 'user'
            ORDER BY d
            """,
            (account_id,),
        ).fetchall()
    return [row["d"] for row in rows if row["d"]]


def list_recent_messages_for_account_since(
    *,
    account_id: str,
    since: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """Return recent context messages for one account since a timestamp, oldest to newest."""
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, role, content, created_at FROM messages
            WHERE account_id = ?
              AND created_at >= ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, since, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def get_latest_message_id_for_account(*, account_id: str) -> Optional[int]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(id) AS id
            FROM messages
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
    if row is None or row["id"] is None:
        return None
    return int(row["id"])


def count_inbound_messages_for_account(*, account_id: str) -> int:
    """账号累计入站(用户)消息总数。

    用于 TDAI 记忆功能（recall + 主动 search 工具）的消息数自然灰度阈值判定：
    只统计 direction='inbound'（用户实际发过的条数），不含 bot 出站。account_id
    有索引，走索引计数，热路径开销可忽略。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
            """,
            (account_id,),
        ).fetchone()
    return int(row["count"] if row else 0)


def count_recent_inbound_messages_for_account(*, account_id: str, since: str) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM messages
            WHERE account_id = ?
              AND direction = 'inbound'
              AND created_at >= ?
            """,
            (account_id, since),
        ).fetchone()
    return int(row["count"] if row else 0)


def list_recent_reactivation_outbound_messages(
    *,
    account_id: str,
    since: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return recent reactivation-origin outbound messages, newest first.

    拉活分类已合并入 companion_followup / content_invitation；拉活来源统一由
    metadata_json.reactivation=true 标识（与 category 解耦），dedupe 据此识别。
    """
    from app.products.zhaoxi.infrastructure.persistence.proactive import _decode_outbound_message
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM outbound_messages
            WHERE account_id = ?
              AND created_at >= ?
              AND status IN ('pending', 'sending', 'sent')
              AND json_valid(metadata_json)
              AND CAST(json_extract(metadata_json, '$.reactivation') AS TEXT) IN ('1', 'true')
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, since, max(int(limit), 1)),
        ).fetchall()
    return [_decode_outbound_message(row) for row in rows]


def count_reactivation_outbound_for_quota_date(
    *,
    account_id: str,
    quota_date: str,
) -> int:
    """Count reactivation-origin outbound rows for a local quota date.

    拉活节流（每天最多 1 次）以 metadata_json.reactivation=true 为准，独立于
    合并后的 companion_followup / content_invitation 分类配额。使用 quota_date
    （入库时按本地日写入）保证跨时区日界正确。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ('pending', 'sending', 'sent')
              AND json_valid(metadata_json)
              AND CAST(json_extract(metadata_json, '$.reactivation') AS TEXT) IN ('1', 'true')
            """,
            (account_id, quota_date),
        ).fetchone()
    return int(row["count"]) if row else 0


def list_reactivation_outbound_messages_admin(
    *,
    account_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return sent reactivation outbound messages across all accounts for admin display.

    Joins with accounts to include display_name. Newest first.
    拉活来源统一以 metadata_json.reactivation=true 标识（分类已合并）。
    """
    from app.products.zhaoxi.infrastructure.persistence.proactive import _decode_outbound_message
    params: List[Any] = []
    account_clause = ""
    if account_id:
        account_clause = "AND o.account_id = ?"
        params.append(account_id)
    since_clause = ""
    if since:
        since_clause = "AND o.created_at >= ?"
        params.append(since)
    params.append(max(int(limit), 1))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT o.*, a.display_name AS account_display_name
            FROM outbound_messages o
            JOIN accounts a ON a.id = o.account_id
            WHERE o.status IN ('pending', 'sending', 'sent')
              {account_clause}
              {since_clause}
              AND json_valid(o.metadata_json)
              AND CAST(json_extract(o.metadata_json, '$.reactivation') AS TEXT) IN ('1', 'true')
            ORDER BY o.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    results = []
    for row in rows:
        item = _decode_outbound_message(row)
        item["account_display_name"] = row["account_display_name"]
        results.append(item)
    return results


def count_context_messages_for_session(*, session_id: int) -> int:
    """Count messages from a session that are eligible for LLM context."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM messages
            WHERE session_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            """,
            (session_id, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR),
        ).fetchone()
    return int(row["count"] if row else 0)


def list_context_messages_for_session(
    *, session_id: int, account_id: str, after_id: int = 0, limit: int = 1000
) -> List[Dict[str, Any]]:
    """Return context-eligible messages of one account/session (id ASC), with id > after_id.

    带 id 返回，供 P3 滚动摘要推进水位线用；显式按 account_id + session_id 双约束，
    避免调用方误传 session_id 时跨账号读上下文。
    """
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, message_id, role, content, created_at FROM messages
            WHERE session_id = ?
              AND account_id = ?
              AND id > ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (
                session_id,
                account_id,
                after_id,
                _NON_CONTEXT_ASSISTANT_REPLY,
                MODERATION_BLOCKED_ERROR,
                limit,
            ),
        ).fetchall()
    # 字段对齐 list_recent_context_messages_for_account（含 message_id 供 tool_evidence、created_at
    # 供时间戳/压缩硬底），使 L0 组装期可直接以 after_id=水位线 取"水位线之后全部消息"。
    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def list_recent_context_messages_for_session(
    *, session_id: int, account_id: str, limit: int
) -> List[Dict[str, Any]]:
    """Return the recent context-eligible tail of one account/session, oldest→newest.

    L0 主取数（统一编排：原始尾窗改为 session-scoped，不再跨 session）。返回字段与
    ``list_recent_messages_for_account`` 对齐（id/session_id/message_id/role/content/created_at），
    以便 tool_evidence 回放、历史时间戳注入、metadata 汇总等下游逻辑无需改动即可复用。
    过滤口径与 ``list_context_messages_for_session`` 一致；取尾部最近 *limit* 条（DESC 后反转）。
    """
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, message_id, role, content, created_at FROM messages
            WHERE session_id = ?
              AND account_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
              AND NOT (
                role = 'assistant'
                AND content = ?
              )
              AND (error IS NULL OR error != ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, account_id, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "message_id": row["message_id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def get_latest_closed_carryover_for_account(
    *, account_id: str, active_session_key: Optional[str] = None
) -> Optional[str]:
    """Return the carryover_summary of the account's **immediately-preceding** closed session.

    取最近一个已关闭 session（不再往前挖）：其 carryover 即新 active session 应承接的上一段延续。
    统一了「懒轮转」与「4 点 scheduler 关闭后懒建」两条路径的 seed 来源（carryover 在两种关闭里
    都已落到被关闭的 session 行上）。若上一段是 `#重置` 之类的 replaced 关闭（无 carryover），返回
    空/None → 不承接（不复活已被显式重置的上下文）。account 隔离。

    ``active_session_key`` 指定 conversation_scope（§7.2，Model B）：传入时只承接**同 scope**
    上一段 carryover（归档 key 形如 ``{active_session_key}:{id}``），避免跨渠道泄漏（微信昨天
    关的 session 被 Web 今天首开继承，反之亦然）。用**前缀精确匹配** ``substr(session_key,1,?)=?``
    而非 ``LIKE``（``LIKE`` 里 ``_`` 是单字符通配符，``__account_active__`` 会误命中）。
    不传（None）= 现状：承接账号最近任意 closed session（微信单渠道下与按 scope 过滤等价，
    因所有归档段都带 ``__account_active__:`` 前缀）。
    """
    with connect() as conn:
        if active_session_key:
            prefix = f"{active_session_key}:"
            row = conn.execute(
                """
                SELECT carryover_summary FROM sessions
                WHERE account_id = ?
                  AND status = 'closed'
                  AND substr(session_key, 1, ?) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (account_id, len(prefix), prefix),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT carryover_summary FROM sessions
                WHERE account_id = ?
                  AND status = 'closed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
    return row["carryover_summary"] if row else None


def update_session_rolling_summary(
    *, session_id: int, rolling_summary: str, rolling_summary_upto_id: int
) -> None:
    """Persist a session's rolling summary text + watermark (P3，account 隔离由 session 归属保证)。"""
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET rolling_summary = ?, rolling_summary_upto_id = ? WHERE id = ?",
            (rolling_summary, rolling_summary_upto_id, session_id),
        )


def clear_session_messages(*, session_id: int) -> int:
    from app.platform.moderation.persistence import _delete_content_moderation_tasks_where
    with connect() as conn:
        _delete_content_moderation_tasks_where(
            conn,
            "session_id = ? OR message_db_id IN (SELECT id FROM messages WHERE session_id = ?)",
            (session_id, session_id),
        )
        cursor = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        return int(cursor.rowcount)


def clear_all_messages_for_account(*, account_id: str) -> int:
    from app.platform.moderation.persistence import _delete_content_moderation_tasks_where
    with connect() as conn:
        _delete_content_moderation_tasks_where(
            conn,
            """
            account_id = ?
            AND (
                session_id IN (SELECT id FROM sessions WHERE account_id = ?)
                OR message_db_id IN (
                    SELECT id FROM messages
                    WHERE session_id IN (SELECT id FROM sessions WHERE account_id = ?)
                )
            )
            """,
            (account_id, account_id, account_id),
        )
        cursor = conn.execute(
            "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE account_id = ?)",
            (account_id,),
        )
        return int(cursor.rowcount)


def list_session_messages(*, session_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, message_id, reply_to_message_id, direction, role,
                message_type, content, latency_ms, error, created_at,
                (
                    SELECT dt.trace_id
                    FROM debug_traces dt
                    WHERE dt.session_id = messages.session_id
                      AND dt.message_id = messages.message_id
                    ORDER BY dt.id DESC
                    LIMIT 1
                ) AS trace_id
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_session_messages_before(
    *,
    account_id: str,
    session_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return one account/session's user-visible text tail, oldest to newest.

    ``account_id`` is deliberately part of the predicate: a caller cannot use a guessed
    numeric session id to read another account's conversation.
    """
    clean_limit = max(1, min(int(limit), 100))
    before_clause = "AND id < ?" if before_id is not None else ""
    params: List[Any] = [session_id, account_id]
    if before_id is not None:
        params.append(int(before_id))
    params.append(clean_limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, message_id, reply_to_message_id, role, message_type, content, created_at
            FROM messages
            WHERE session_id = ?
              AND account_id = ?
              AND role IN ('user', 'assistant')
              AND content IS NOT NULL
              AND content != ''
              {before_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_app_conversation_messages_before(
    *,
    runtime_account_id: str,
    before_id: Optional[int] = None,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """跨业务日读取一个 runtime account 的 App 私聊历史，按页内时间升序返回。

    只接受 ``__app_active__`` 当前段及 ``__app_active__:<session_id>`` 已归档段；微信、Web
    和同世界其他 resident 的 session 均不命中。查询同时约束 message/session 的 account_id，
    即使调用方误传或库内存在脏关联也不会跨账号读取。
    """
    clean_limit = max(1, min(int(limit), 100))
    prefix = f"{APP_ACTIVE_SESSION_KEY}:"
    before_clause = "AND m.id < ?" if before_id is not None else ""
    params: List[Any] = [
        runtime_account_id,
        runtime_account_id,
        APP_ACTIVE_SESSION_KEY,
        len(prefix),
        prefix,
    ]
    if before_id is not None:
        params.append(int(before_id))
    params.append(clean_limit)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT m.id, m.message_id, m.reply_to_message_id, m.role,
                   m.message_type, m.content, m.created_at
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.account_id = ? AND s.account_id = ?
              AND (
                    s.session_key = ?
                    OR substr(s.session_key, 1, ?) = ?
              )
              AND m.role IN ('user', 'assistant')
              AND m.content IS NOT NULL AND m.content != ''
              {before_clause}
            ORDER BY m.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def summarize_app_conversations(
    *,
    read_cursors: Mapping[str, Optional[int]],
    conn: Optional[Connection] = None,
) -> Dict[str, Dict[str, Any]]:
    """批量汇总一页会话的「最近一条 + 未读数」，用两条 SQL 取代逐会话查询（CONV-001）。

    ``read_cursors`` 是 ``runtime_account_id -> last_read_message_id``（``None`` = 一条都没读过）。
    返回 ``{runtime_account_id: {last_message_id, last_preview, last_message_at, unread}}``；
    该 account 在 App scope 下没有任何消息时不出现在结果里，由调用方按「未聊过」处理。

    session scope 与 :func:`list_app_conversation_messages_before` 完全一致：只认
    ``__app_active__`` 当前段与 ``__app_active__:<session_id>`` 归档段，微信/Web 的 session
    不参与预览与未读；message 与 session 的 account_id 同时约束，杜绝跨账号读取。
    """
    account_ids = [str(account_id) for account_id in read_cursors]
    if not account_ids:
        return {}
    prefix = f"{APP_ACTIVE_SESSION_KEY}:"
    scope_clause = """
        JOIN sessions s ON s.id = m.session_id
        WHERE s.account_id = m.account_id
          AND (s.session_key = ? OR substr(s.session_key, 1, ?) = ?)
          AND m.content IS NOT NULL AND m.content != ''
    """
    scope_params: List[Any] = [APP_ACTIVE_SESSION_KEY, len(prefix), prefix]
    placeholders = ",".join("?" for _ in account_ids)
    summary: Dict[str, Dict[str, Any]] = {}
    with _tx(conn) as tx:
        latest_rows = tx.execute(
            f"""
            SELECT m.account_id, m.id, m.content, m.created_at
            FROM messages m
            JOIN (
                SELECT m.account_id AS account_id, MAX(m.id) AS id
                FROM messages m
                {scope_clause}
                  AND m.role IN ('user', 'assistant')
                  AND m.account_id IN ({placeholders})
                GROUP BY m.account_id
            ) latest ON latest.id = m.id
            """,
            tuple([*scope_params, *account_ids]),
        ).fetchall()
        for row in latest_rows:
            summary[str(row["account_id"])] = {
                "last_message_id": int(row["id"]),
                "last_preview": row["content"],
                "last_message_at": row["created_at"],
                "unread": 0,
            }
        # 每个会话的未读起点不同，用 (account_id, cursor) 成对条件一次问完；一页最多 100 组。
        cursor_clauses: List[str] = []
        cursor_params: List[Any] = []
        for account_id in account_ids:
            cursor_clauses.append("(m.account_id = ? AND m.id > ?)")
            cursor_params.extend([account_id, int(read_cursors[account_id] or 0)])
        unread_rows = tx.execute(
            f"""
            SELECT m.account_id AS account_id, COUNT(*) AS unread
            FROM messages m
            {scope_clause}
              AND m.role = 'assistant'
              AND ({' OR '.join(cursor_clauses)})
            GROUP BY m.account_id
            """,
            tuple([*scope_params, *cursor_params]),
        ).fetchall()
    for row in unread_rows:
        entry = summary.get(str(row["account_id"]))
        if entry is not None:
            entry["unread"] = int(row["unread"])
    return summary


def list_recent_message_raw(*, limit: int = 20) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.id,
                m.account_id,
                m.session_id,
                m.message_id,
                m.direction,
                m.role,
                m.message_type,
                m.content,
                m.raw_json,
                m.created_at,
                s.session_key
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.direction = 'inbound'
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_decode_raw_message(row) for row in rows]


def get_message_raw(*, message_db_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                m.id,
                m.account_id,
                m.session_id,
                m.message_id,
                m.direction,
                m.role,
                m.message_type,
                m.content,
                m.raw_json,
                m.created_at,
                s.session_key
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.id = ?
            """,
            (message_db_id,),
        ).fetchone()
    return _decode_raw_message(row) if row else None


def _decode_raw_message(row: Row) -> Dict[str, Any]:
    item = dict(row)
    raw_json = item.pop("raw_json", None)
    try:
        item["raw"] = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        item["raw"] = {"_decode_error": True, "raw_json": raw_json}
    return item


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def list_sessions(*, limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.account_id,
                s.session_key,
                s.sender_id,
                s.chat_id,
                s.sender_name,
                s.status,
                s.ended_at,
                s.close_reason,
                s.turn_count,
                s.business_day,
                s.session_summary,
                s.carryover_summary,
                s.summary_model,
                s.summary_prompt_version,
                s.metadata_json,
                s.created_at,
                s.updated_at,
                p.style,
                p.display_name AS profile_display_name,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM sessions s
            LEFT JOIN profiles p ON p.account_id = s.account_id
            LEFT JOIN messages m ON m.session_id = s.id
            -- GROUP BY 含 p.* 列：PG 仅对「按主键分组的同表列」放行函数依赖，跨表
            -- profiles 列须显式入组（1:1 关系，结果不变）；ORDER BY 用底层聚合表达式
            -- 而非 SELECT 别名（PG 不允许别名出现在 ORDER BY 表达式内）。
            GROUP BY s.id, p.style, p.display_name
            ORDER BY COALESCE(MAX(m.created_at), s.updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def get_session_for_account_and_key(
    *, account_id: str, session_key: str
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM sessions
            WHERE account_id = ? AND session_key = ? AND status = 'active'
            """,
            (account_id, session_key),
        ).fetchone()
    return dict(row) if row else None


def get_account_id_for_session_key(*, session_key: str) -> Optional[str]:
    """按 session_key 反查已存在的 account_id（不校验 binding）。

    仅供 openclaw_inbound_require_binding=False 的本地/测试兜底路径使用：
    该开关关闭时，无 completed binding 的入站会直接把 session_key 当新
    account_id，若此前已有账号（如 debug 建号）用同一 session_key 落过
    session，会导致每次兜底都新建一个不同的影子账号。这里让兜底先复用
    已存在的账号，只有真的从未出现过这个 session_key 时才新建。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM sessions WHERE session_key = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
    return str(row["account_id"]) if row else None


def list_sessions_for_account(*, account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.account_id,
                s.session_key,
                s.sender_id,
                s.chat_id,
                s.sender_name,
                s.status,
                s.ended_at,
                s.close_reason,
                s.turn_count,
                s.business_day,
                s.session_summary,
                s.carryover_summary,
                s.summary_model,
                s.summary_prompt_version,
                s.metadata_json,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_message_at
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.account_id = ?
            GROUP BY s.id
            -- ORDER BY 用底层聚合表达式而非 SELECT 别名（PG 不允许别名出现在表达式内）
            ORDER BY COALESCE(MAX(m.created_at), s.updated_at) DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_accounts() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.app_id,
                a.channel,
                a.display_name,
                a.status,
                a.notes,
                a.daily_limit,
                a.rpm_limit,
                a.created_at,
                a.updated_at,
                COUNT(DISTINCT s.id) AS session_count,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_active_at
            FROM accounts a
            LEFT JOIN sessions s ON s.account_id = a.id
            LEFT JOIN messages m ON m.account_id = a.id
            GROUP BY a.id
            ORDER BY a.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_account(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                a.id,
                a.app_id,
                a.channel,
                a.display_name,
                a.status,
                a.notes,
                a.daily_limit,
                a.rpm_limit,
                a.is_debug,
                a.created_at,
                a.updated_at,
                COUNT(DISTINCT s.id) AS session_count,
                COUNT(m.id) AS message_count,
                MAX(m.created_at) AS last_active_at
            FROM accounts a
            LEFT JOIN sessions s ON s.account_id = a.id
            LEFT JOIN messages m ON m.account_id = a.id
            WHERE a.id = ?
            GROUP BY a.id
            """,
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def get_account_product_access(*, account_id: str) -> Optional[Dict[str, Any]]:
    """返回账号、真人 owner 与同产品 membership 状态，供入口无副作用鉴权。"""
    with connect() as conn:
        account = conn.execute(
            "SELECT id, app_id FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            return None
        platform_user_id = resolve_owner_platform_user_id(conn, account_id)
        membership = None
        if platform_user_id is not None:
            membership = conn.execute(
                """
                SELECT status
                FROM product_memberships
                WHERE platform_user_id = ? AND app_id = ?
                """,
                (platform_user_id, account["app_id"]),
            ).fetchone()
    return {
        "account_id": str(account["id"]),
        "app_id": str(account["app_id"]),
        "platform_user_id": platform_user_id,
        "membership_status": str(membership["status"]) if membership else None,
    }


def update_account(
    *,
    account_id: str,
    display_name=_UNSET,
    notes=_UNSET,
    daily_limit=_UNSET,
    rpm_limit=_UNSET,
) -> Optional[Dict[str, Any]]:
    current = get_account(account_id=account_id)
    if current is None:
        return None
    with connect() as conn:
        platform_user_id = resolve_owner_platform_user_id(conn, account_id)
        app_id = str(current["app_id"])
        next_daily = current.get("daily_limit") if daily_limit is _UNSET else daily_limit
        next_rpm = current.get("rpm_limit") if rpm_limit is _UNSET else rpm_limit
        conn.execute(
            """
            UPDATE accounts
            SET display_name = ?,
                notes = ?,
                daily_limit = ?,
                rpm_limit = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                current.get("display_name") if display_name is _UNSET else display_name,
                current.get("notes") if notes is _UNSET else notes,
                next_daily,
                next_rpm,
                account_id,
            ),
        )
        if platform_user_id is not None and (
            daily_limit is not _UNSET or rpm_limit is not _UNSET
        ):
            membership_sets = []
            membership_params = []
            if daily_limit is not _UNSET:
                membership_sets.append("daily_limit = ?")
                membership_params.append(daily_limit)
            if rpm_limit is not _UNSET:
                membership_sets.append("rpm_limit = ?")
                membership_params.append(rpm_limit)
            membership_sets.append(
                "updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))"
            )
            updated_membership = conn.execute(
                f"UPDATE product_memberships SET {', '.join(membership_sets)} "
                "WHERE platform_user_id = ? AND app_id = ? AND status = 'active'",
                (*membership_params, platform_user_id, app_id),
            )
            if getattr(updated_membership, "rowcount", 0) != 1:
                raise ValueError("active product membership required")

            # account 列只保留兼容投影；产品级 canonical 值只写 membership。
            owned_ids = _account_ids_for_platform_user(
                conn,
                platform_user_id,
                app_id=app_id,
            )
            if owned_ids:
                placeholders = ", ".join("?" for _ in owned_ids)
                account_sets = []
                account_params = []
                if daily_limit is not _UNSET:
                    account_sets.append("daily_limit = ?")
                    account_params.append(daily_limit)
                if rpm_limit is not _UNSET:
                    account_sets.append("rpm_limit = ?")
                    account_params.append(rpm_limit)
                account_sets.append(
                    "updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))"
                )
                conn.execute(
                    f"UPDATE accounts SET {', '.join(account_sets)} "
                    f"WHERE id IN ({placeholders})",
                    (*account_params, *owned_ids),
                )
    return get_account(account_id=account_id)


def set_account_debug_flag(*, account_id: str, is_debug: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET is_debug = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) WHERE id = ?",
            (1 if is_debug else 0, account_id),
        )


def set_account_status(
    *,
    account_id: str,
    status: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Optional[Dict[str, Any]]:
    from app.db.billing import get_platform_user_id_for_account, retry_qualified_referral_rewards_for_user
    current = get_account(account_id=account_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) WHERE id = ?",
            (status, account_id),
        )
    if status == "active":
        platform_user_id = get_platform_user_id_for_account(account_id=account_id)
        if platform_user_id:
            retry_qualified_referral_rewards_for_user(
                platform_user_id=platform_user_id,
                app_id=str(current["app_id"]),
                registry=registry,
            )
    return get_account(account_id=account_id)


def get_account_onboarding_state(*, account_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT onboarding_state FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    if row is None:
        return "pending"
    return row["onboarding_state"] or "pending"


def get_account_last_inbound_at(*, account_id: str, channel: str) -> Optional[str]:
    """Return the most recent `channel_bindings.last_seen_at` for this account **on
    the given channel**, or None if it has no binding on that channel.

    Only real inbound turns refresh `last_seen_at` (see `upsert_channel_binding`
    callers) — unlike the `messages`-table `MAX(created_at)` aggregates used
    elsewhere for display, this is not contaminated by the bot's own outbound
    messages, so it is a clean signal for "when did this account last send an
    inbound message on this channel" (used by new-user-reactivation idle-hours
    eligibility and the WeChat proactive touch window).

    ``channel`` is **mandatory** (Model B, §7.5): a WeChat proactive-reachability
    check must not be contaminated by activity on other channels (e.g. Web). The
    two current callers both pass ``channel="openclaw-weixin"``. (Previously there
    were two identical channel-agnostic definitions of this function, the second
    shadowing the first; they have been converged into this single channel-scoped
    definition.)
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(last_seen_at) AS last_seen_at FROM channel_bindings "
            "WHERE account_id = ? AND channel = ?",
            (account_id, channel),
        ).fetchone()
    return row["last_seen_at"] if row and row["last_seen_at"] else None


def record_analytics_event(
    *,
    account_id: str,
    event_name: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    source: Optional[str] = None,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """记录一条结构化分析事件（append-only）。

    仅写元数据（状态/来源/枚举/布尔），严禁写入用户正文（昵称、自定义人设描述等）。
    供运营分析消费；调用方应自行容错，打点失败不得影响业务主流程。
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO analytics_events"
            "(account_id, event_name, from_state, to_state, source, properties_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                account_id,
                event_name,
                from_state,
                to_state,
                source,
                json.dumps(properties or {}, ensure_ascii=False),
            ),
        )


def set_account_onboarding_state(*, account_id: str, state: str) -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT onboarding_state FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        from_state = (row["onboarding_state"] if row else None) or "pending"
        conn.execute(
            "UPDATE accounts SET onboarding_state = ?, onboarding_updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) WHERE id = ?",
            (state, account_id),
        )
    # 旁路打点：记录状态转移（仅在状态实际变化时）。失败不影响 onboarding 主流程。
    if from_state != state:
        try:
            record_analytics_event(
                account_id=account_id,
                event_name="onboarding_state_changed",
                from_state=from_state,
                to_state=state,
                source="turn_service",
            )
        except Exception as err:
            logger.warning("analytics onboarding event emit failed account=%s error=%s", account_id, err)


def resolve_owner_platform_user_id(cursor, account_id: str) -> Optional[str]:
    """把 account_id 解析为它归属的真人 platform_user_id（canonical，两形态统一收口）。

    ⚠️ 「account」一词两义（历史命名债，详见
    docs/tech_design/companion_world_account_model_reconciliation.md §1）：
      - 形态 A（微信接入）：account 经 account_owner_bindings 绑到真人。**owner_binding 是
        微信接入独有的产物**，记录「微信渠道把某 account 绑到某真人」，不是通用「用户账号」机制。
      - 形态 B（朝夕相伴 App）：真人的世界里每个 AI 居民 = 一行 runtime account
        （universe_residents.runtime_account_id → universes.owner_platform_user_id），
        **不发 owner_binding**；纯朝夕相伴用户零 owner_binding。

    解析链（账号模型对齐决策 B）：
      1) account_owner_bindings 最早 active binding 的 platform_user_id（形态 A）；
      2) 无则走「世界归属」：该 account 作为居民 runtime account 所属 universe 的 owner（形态 B）；
      3) 都无 → None（孤儿号；调用方决定是否回退 account_id 本身）。

    刻意在给定连接/事务上查，供写事务内复用、避免嵌套开新连接。form-A 存量账号世界表恒空、
    走 (1) 即返回，行为与上迁前一致（零回归）。迁移期回填只认 owner_binding（居民为增量、
    迁移时不存在），故仅**运行时**解析需要 (2) 这段 fallback。
    """
    row = cursor.execute(
        """
        SELECT platform_user_id
        FROM account_owner_bindings
        WHERE account_id = ? AND status = 'active'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if row is not None:
        return str(row["platform_user_id"])
    # 形态 B：朝夕相伴居民 runtime account 无 owner_binding，经世界归属解析到真人。
    # ux_universe_residents_runtime 保证 runtime_account_id 唯一（WHERE 非空），至多命中一行。
    world = cursor.execute(
        """
        SELECT u.owner_platform_user_id AS owner
        FROM universe_residents r
        JOIN universes u ON u.id = r.universe_id
        WHERE r.runtime_account_id = ?
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if world is not None and world["owner"] is not None:
        return str(world["owner"])
    return None


def _account_ids_for_platform_user(
    cursor, platform_user_id: str, *, app_id: str
) -> List[str]:
    """返回真人在指定产品归属的入口账号与 World resident runtime accounts。"""
    rows = cursor.execute(
        """
        SELECT b.account_id
        FROM account_owner_bindings b
        JOIN accounts a ON a.id=b.account_id
        WHERE b.platform_user_id = ? AND b.status = 'active'
          AND b.app_id = ? AND a.app_id = ?
        UNION
        SELECT r.runtime_account_id AS account_id
        FROM universe_residents r
        JOIN universes u ON u.id = r.universe_id
        JOIN accounts a ON a.id=r.runtime_account_id
        WHERE u.owner_platform_user_id = ? AND r.runtime_account_id IS NOT NULL
          AND a.app_id = ?
        """,
        (platform_user_id, app_id, app_id, platform_user_id, app_id),
    ).fetchall()
    return [str(row["account_id"]) for row in rows]


def resolve_effective_quota_limits(
    *, account_id: str, default_daily: int, default_rpm: int
) -> Dict[str, Any]:
    """从产品 membership 解析 daily/RPM 上限；孤儿号保留 per-account 兼容。"""
    with connect() as conn:
        account = conn.execute(
            "SELECT app_id, daily_limit, rpm_limit FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            return {
                "daily_limit": int(default_daily),
                "rpm_limit": int(default_rpm),
                "source": "default",
            }
        app_id = str(account["app_id"])
        scope = _resolve_quota_scope(conn, account_id)
        assert scope is not None
        platform_user_id = scope["platform_user_id"]
        if platform_user_id is None:
            return {
                "daily_limit": int(
                    default_daily
                    if account["daily_limit"] is None
                    else account["daily_limit"]
                ),
                "rpm_limit": int(
                    default_rpm if account["rpm_limit"] is None else account["rpm_limit"]
                ),
                "source": "orphan_account",
                "app_id": app_id,
                "quota_subject": scope["subject"],
            }
        membership = conn.execute(
            """
            SELECT daily_limit, rpm_limit
            FROM product_memberships
            WHERE platform_user_id=? AND app_id=? AND status='active'
            """,
            (platform_user_id, app_id),
        ).fetchone()
        if membership is None:
            raise ValueError("active product membership required")
        daily = int(
            default_daily
            if membership["daily_limit"] is None
            else membership["daily_limit"]
        )
        rpm = int(
            default_rpm
            if membership["rpm_limit"] is None
            else membership["rpm_limit"]
        )
        return {
            "daily_limit": daily,
            "rpm_limit": rpm,
            "source": "product_membership",
            "platform_user_id": platform_user_id,
            "app_id": app_id,
            "quota_subject": scope["subject"],
        }


def _resolve_quota_scope(
    cursor, account_id: str, *, allow_missing: bool = False
) -> Optional[Dict[str, Optional[str]]]:
    """从 account 推导产品配额锚；真人账号必须有同产品 active membership。"""
    account = cursor.execute(
        "SELECT id, app_id FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if account is None:
        if allow_missing:
            return None
        raise ValueError("account not found")
    app_id = str(account["app_id"])
    platform_user_id = resolve_owner_platform_user_id(cursor, account_id)
    if platform_user_id is None:
        return {
            "platform_user_id": None,
            "subject": account_id,
            "app_id": app_id,
        }
    owner_in_app = cursor.execute(
        """
        SELECT 1
        FROM account_owner_bindings b
        WHERE b.account_id=? AND b.platform_user_id=? AND b.status='active'
          AND b.app_id=?
        UNION ALL
        SELECT 1
        FROM universe_residents r
        JOIN universes u ON u.id=r.universe_id
        WHERE r.runtime_account_id=? AND u.owner_platform_user_id=?
        LIMIT 1
        """,
        (account_id, platform_user_id, app_id, account_id, platform_user_id),
    ).fetchone()
    membership = cursor.execute(
        """
        SELECT status FROM product_memberships
        WHERE platform_user_id=? AND app_id=?
        """,
        (platform_user_id, app_id),
    ).fetchone()
    if owner_in_app is None:
        raise ValueError("quota scope mismatch")
    if membership is None or membership["status"] != "active":
        raise ValueError("active product membership required")
    return {
        "platform_user_id": platform_user_id,
        "subject": platform_user_id,
        "app_id": app_id,
    }


def get_daily_usage(*, account_id: str, date: str) -> int:
    with connect() as conn:
        scope = _resolve_quota_scope(conn, account_id, allow_missing=True)
        if scope is None:
            return 0
        row = conn.execute(
            """
            SELECT message_count FROM daily_usage
            WHERE platform_user_id=? AND app_id=? AND date=?
            """,
            (scope["subject"], scope["app_id"], date),
        ).fetchone()
    return int(row["message_count"]) if row else 0


def _increment_daily_usage_in_scope(
    tx: Connection,
    *,
    account_id: str,
    platform_user_id: str,
    app_id: str,
    date: str,
) -> int:
    """在已校验产品作用域内原子累加 daily usage。"""
    tx.execute(
        """
        INSERT INTO daily_usage(
            account_id, platform_user_id, app_id, date, message_count, updated_at
        )
        VALUES (?, ?, ?, ?, 1, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        ON CONFLICT(platform_user_id, app_id, date) DO UPDATE SET
            message_count = daily_usage.message_count + 1,
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        """,
        (account_id, platform_user_id, app_id, date),
    )
    row = tx.execute(
        """
        SELECT message_count FROM daily_usage
        WHERE platform_user_id=? AND app_id=? AND date=?
        """,
        (platform_user_id, app_id, date),
    ).fetchone()
    return int(row["message_count"]) if row else 1


def increment_daily_usage(
    *,
    account_id: str,
    date: str,
    conn: Optional[Connection] = None,
) -> int:
    with _tx(conn) as tx:
        scope = _resolve_quota_scope(tx, account_id)
        assert scope is not None
        return _increment_daily_usage_in_scope(
            tx,
            account_id=account_id,
            platform_user_id=str(scope["subject"]),
            app_id=str(scope["app_id"]),
            date=date,
        )


def get_usage_last_7_days(*, account_id: str) -> List[Dict[str, Any]]:
    # D-09：展示与强制一致，按真人聚合的近 7 日用量。
    with connect() as conn:
        scope = _resolve_quota_scope(conn, account_id, allow_missing=True)
        if scope is None:
            return []
        rows = conn.execute(
            """
            SELECT date, message_count FROM daily_usage
            WHERE platform_user_id=? AND app_id=?
            ORDER BY date DESC
            LIMIT 7
            """,
            (scope["subject"], scope["app_id"]),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Daily 配额原子预占 / 回滚 / TTL 回收（D-09 下半刀）
#
# 消除 turn_service 的「读—处理—+1」TOCTOU：reserve 在 advisory 锁下按
# message_count + 活跃 reservation 数原子校验 cap 后预占一格，仅真正成功计费的 turn
# 才 confirm（计入 message_count），moderation 拦截/模型失败/未计费一律 rollback（退款
# 矩阵）。崩溃悬挂 reservation 由 TTL（prune-on-touch + 调度器批量）回收。
# 见 ADR §D-09 item 3–6、P1 §2.7 锁序 L3。
# ---------------------------------------------------------------------------

def reserve_daily_quota(
    *,
    account_id: str,
    date: str,
    limit: int,
    ttl_minutes: int = 15,
    conn: Optional[Connection] = None,
) -> Optional[str]:
    """原子预占一格 daily 配额，返回 reservation token；已满返回 None（limit<=0 不限、必得 token）。

    强制口径 = daily_usage.message_count（已确认）+ 当日活跃 reservation 数 < limit。PG 路径先取单键
    事务级 advisory 锁（'quota:'||platform_user）串行化同真人的检查-预占，消除 TOCTOU/超卖；SQLite
    单写者天然串行。预占行带 expires_at = now + ttl_minutes，崩溃悬挂由 TTL 回收。account_id 内部解析
    成 platform_user 聚合键（孤儿号回退 account_id）。

    调用契约：去重（insert_message 幂等）须由调用方**先于**本函数、**同一 conn** 内完成
    （ADR「去重先于预占同事务」，重试不吃配额）。
    """
    with _tx(conn) as tx:
        scope = _resolve_quota_scope(tx, account_id)
        assert scope is not None
        subject = str(scope["subject"])
        app_id = str(scope["app_id"])
        if is_postgres():
            tx.execute(
                "SELECT pg_advisory_xact_lock(?)",
                (
                    advisory_lock_key(
                        "quota:"
                        + product_quota_subject(
                            platform_user_id=subject,
                            app_id=app_id,
                        )
                    ),
                ),
            )
        tx.execute(
            "DELETE FROM daily_quota_reservations WHERE platform_user_id=? AND app_id=? "
            "AND expires_at <= strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))",
            (subject, app_id),
        )
        if limit > 0:
            used_row = tx.execute(
                """
                SELECT message_count FROM daily_usage
                WHERE platform_user_id=? AND app_id=? AND date=?
                """,
                (subject, app_id, date),
            ).fetchone()
            used = int(used_row["message_count"]) if used_row else 0
            reserved_row = tx.execute(
                "SELECT COUNT(*) AS cnt FROM daily_quota_reservations "
                "WHERE platform_user_id=? AND app_id=? AND date=?",
                (subject, app_id, date),
            ).fetchone()
            reserved = int(reserved_row["cnt"]) if reserved_row else 0
            if used + reserved >= limit:
                return None
        reservation_id = _new_id("qres")
        tx.execute(
            """
            INSERT INTO daily_quota_reservations(
                id, platform_user_id, app_id, date, account_id, expires_at
            )
            VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?)))
            """,
            (
                reservation_id,
                subject,
                app_id,
                date,
                account_id,
                f"+{int(ttl_minutes)} minutes",
            ),
        )
    return reservation_id


def confirm_daily_quota(*, reservation_id: Optional[str], conn: Optional[Connection] = None) -> None:
    """确认预占：删预占行并把该次消耗计入 daily_usage.message_count。

    幂等：reservation 行已删（二次确认 / 已回滚 / 已 TTL 回收）→ no-op、不双记（DELETE rowcount 守门）。
    reservation_id 为 None（该 turn 未预占，如 onboarding welcome）→ no-op。
    """
    if not reservation_id:
        return
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT account_id, platform_user_id, app_id, date
            FROM daily_quota_reservations WHERE id=?
            """,
            (reservation_id,),
        ).fetchone()
        if row is None:
            return
        scope = _resolve_quota_scope(tx, str(row["account_id"]))
        assert scope is not None
        if (
            scope["subject"] != row["platform_user_id"]
            or scope["app_id"] != row["app_id"]
        ):
            raise ValueError("quota reservation scope mismatch")
        cur = tx.execute("DELETE FROM daily_quota_reservations WHERE id = ?", (reservation_id,))
        if getattr(cur, "rowcount", 0) != 1:
            return  # 并发下已被他方删除 → 不重复计数
        _increment_daily_usage_in_scope(
            tx,
            account_id=str(row["account_id"]),
            platform_user_id=str(row["platform_user_id"]),
            app_id=str(row["app_id"]),
            date=str(row["date"]),
        )


def rollback_daily_quota(*, reservation_id: Optional[str], conn: Optional[Connection] = None) -> None:
    """回滚预占：删预占行、不计入 message_count（退款矩阵：未成功计费的 turn）。

    幂等（删缺失 = no-op）。reservation_id 为 None → no-op。
    """
    if not reservation_id:
        return
    with _tx(conn) as tx:
        tx.execute("DELETE FROM daily_quota_reservations WHERE id = ?", (reservation_id,))


def reclaim_expired_reservations(*, now: str, limit: int = 200) -> int:
    """批量回收已过期的悬挂预占（崩溃后再无来信、prune-on-touch 覆盖不到的残留）。返回清理条数。

    now：北京时间字符串（'YYYY-MM-DD HH:MM:SS'），与 expires_at 列同格式做字典序（=按时间）比较。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM daily_quota_reservations WHERE expires_at <= ? "
            "ORDER BY expires_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM daily_quota_reservations WHERE id IN ({placeholders})",
                ids,
            )
    return len(ids)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def get_profile_for_session(*, session_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM profiles p
            JOIN sessions s ON s.account_id = p.account_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def get_profile_for_account(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return dict(row) if row else None


def update_profile_for_account(
    *,
    account_id: str,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_profile_for_account(account_id=account_id)
    if current is None:
        return None

    next_preferences = preferences
    if next_preferences is None:
        try:
            next_preferences = json.loads(current.get("preferences_json") or "{}")
        except json.JSONDecodeError:
            next_preferences = {}

    with connect() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET display_name = ?,
                style = ?,
                system_prompt = ?,
                preferences_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ?
            """,
            (
                display_name if display_name is not None else current.get("display_name"),
                style if style is not None else current.get("style"),
                system_prompt if system_prompt is not None else current.get("system_prompt"),
                json.dumps(next_preferences, ensure_ascii=False),
                account_id,
            ),
        )
    return get_profile_for_account(account_id=account_id)


def update_profile_for_session(
    *,
    session_id: int,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
    system_prompt: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    session = get_session(session_id=session_id)
    if session is None:
        return None
    return update_profile_for_account(
        account_id=session["account_id"],
        display_name=display_name,
        style=style,
        system_prompt=system_prompt,
        preferences=preferences,
    )


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

def normalize_phone(phone: str) -> str:
    return _normalize_phone(phone)


def create_phone_verification(
    *,
    phone: str,
    code: str,
    expires_minutes: int,
) -> Dict[str, Any]:
    normalized = _normalize_phone(phone)
    verification_id = _new_id("phv")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO phone_verifications(id, phone, code, expires_at, created_at)
            VALUES (?, ?, ?, datetime('now', '+8 hours', ? || ' minutes'), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (verification_id, normalized, code, f"+{expires_minutes}"),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row)


def get_latest_active_verification(phone: str) -> Optional[Dict[str, Any]]:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE phone = ?
              AND verified_at IS NULL
              AND expires_at > datetime('now', '+8 hours')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return dict(row) if row else None


def count_verifications_last_hour(phone: str) -> int:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM phone_verifications
            WHERE phone = ?
              AND created_at > datetime('now', '+8 hours', '-1 hour')
            """,
            (normalized,),
        ).fetchone()
    return int(row[0]) if row else 0


def invalidate_verifications_for_phone(phone: str) -> None:
    """Expire all active OTP verification rows for a phone number."""
    normalized = _normalize_phone(phone)
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET expires_at = datetime('now', '+8 hours', '-1 second')
            WHERE phone = ?
              AND expires_at > datetime('now', '+8 hours')
            """,
            (normalized,),
        )


def invalidate_other_verifications_for_phone(phone: str, keep_id: str) -> None:
    """Expire active OTP verification rows for a phone number except the row to keep."""
    normalized = _normalize_phone(phone)
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET expires_at = datetime('now', '+8 hours', '-1 second')
            WHERE phone = ?
              AND id != ?
              AND expires_at > datetime('now', '+8 hours')
            """,
            (normalized, keep_id),
        )


def invalidate_verification(verification_id: str) -> None:
    """Expire one OTP verification row by id."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET expires_at = datetime('now', '+8 hours', '-1 second')
            WHERE id = ?
            """,
            (verification_id,),
        )


def increment_verify_attempts(verification_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verify_attempts = verify_attempts + 1
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def set_verification_verified(
    verification_id: str,
    *,
    token_expires_minutes: int,
) -> Optional[Dict[str, Any]]:
    token = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verified_at = datetime('now', '+8 hours'),
                verified_token = ?,
                token_expires_at = datetime('now', '+8 hours', ? || ' minutes')
            WHERE id = ?
            """,
            (token, f"+{token_expires_minutes}", verification_id),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def get_verification_by_token(verified_token: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE verified_token = ?",
            (verified_token,),
        ).fetchone()
    return dict(row) if row else None


def consume_verification_token(verification_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET token_consumed_at = datetime('now', '+8 hours')
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?",
            (verification_id,),
        ).fetchone()
    return dict(row) if row else None


def get_valid_verification_by_token(
    verified_token: str,
    phone: str,
) -> Optional[Dict[str, Any]]:
    normalized = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE verified_token = ?
              AND phone = ?
              AND token_consumed_at IS NULL
              AND token_expires_at > datetime('now', '+8 hours')
            """,
            (verified_token, normalized),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Platform user sessions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPrincipal:
    """服务端验证后的 session 身份与产品 audience。"""

    session_id: str
    platform_user_id: str
    app_id: str
    expires_at: str


def create_platform_user_session(
    *,
    platform_user_id: str,
    days: int = 7,
    app_id: str = ZHAOXI_APP_ID,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    import secrets
    registered_app_id = registry.require_enabled(app_id).app_id
    token = secrets.token_urlsafe(32)
    session_id = f"sess_{uuid.uuid4().hex}"
    with connect() as conn:
        _require_active_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=registered_app_id,
            registry=registry,
        )
        conn.execute(
            """
            INSERT INTO platform_user_sessions(
                id, platform_user_id, app_id, token, expires_at
            )
            VALUES (?, ?, ?, ?, datetime('now', '+8 hours', ? || ' days'))
            """,
            (session_id, platform_user_id, registered_app_id, token, f"+{days}"),
        )
        row = conn.execute(
            "SELECT * FROM platform_user_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row)


def resolve_session_principal(
    *,
    token: str,
    expected_app_id: Optional[str] = None,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Optional[SessionPrincipal]:
    """验证 token、产品注册、audience 与 active membership 后返回 principal。"""

    cleaned_token = str(token or "").strip()
    if not cleaned_token:
        return None
    registered_expected = None
    if expected_app_id is not None:
        try:
            registered_expected = registry.require_enabled(expected_app_id).app_id
        except ValueError:
            return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.platform_user_id, s.app_id, s.expires_at
            FROM platform_user_sessions s
            JOIN platform_users pu ON pu.id=s.platform_user_id
            JOIN product_memberships pm
              ON pm.platform_user_id=s.platform_user_id AND pm.app_id=s.app_id
            WHERE s.token=?
              AND s.expires_at > datetime('now', '+8 hours')
              AND pm.status='active'
            """,
            (cleaned_token,),
        ).fetchone()
    if row is None:
        return None
    try:
        registered_session_app = registry.require_enabled(str(row["app_id"])).app_id
    except ValueError:
        return None
    if registered_expected is not None and registered_session_app != registered_expected:
        return None
    return SessionPrincipal(
        session_id=str(row["session_id"]),
        platform_user_id=str(row["platform_user_id"]),
        app_id=registered_session_app,
        expires_at=str(row["expires_at"]),
    )


def revoke_platform_user_session(*, token: str) -> bool:
    """Revoke exactly the presented opaque mobile/web session token."""
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM platform_user_sessions WHERE token = ?",
            (token,),
        )
    return bool(cursor.rowcount)


def consume_valid_verification_token(
    verified_token: str,
    phone: str,
) -> Optional[Dict[str, Any]]:
    """Atomically consume a verified token. Returns the row if it was valid and not yet consumed, None otherwise."""
    normalized = _normalize_phone(phone)
    with connect() as conn:
        # rowcount 取本次 UPDATE 影响行数（sqlite3/psycopg 一致），替代 SQLite 专有 changes()
        cursor = conn.execute(
            """
            UPDATE phone_verifications
            SET token_consumed_at = datetime('now', '+8 hours')
            WHERE verified_token = ?
              AND phone = ?
              AND token_consumed_at IS NULL
              AND token_expires_at > datetime('now', '+8 hours')
            """,
            (verified_token, normalized),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE verified_token = ? AND phone = ?",
            (verified_token, normalized),
        ).fetchone()
    return dict(row) if row else None
