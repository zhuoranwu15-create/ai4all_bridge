"""app.products.zhaoxi.infrastructure.persistence.proactive — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import Row
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.db._core import (
    _UNSET,
    _clean_text,
    _new_id,
    connect,
)
# 豁免分类以 registry 为单一数据源，避免与 categories.py 漂移（新增豁免分类即生效）。
# categories.py 为纯数据叶子模块，不反向依赖 app.db，故此 import 无环。
from app.products.zhaoxi.proactive.contract.categories import EXEMPT_CATEGORIES
__all__ = [
    'CONTENT_INVITATION_ACTIVE_STATUSES',
    'OUTBOUND_QUOTA_STATUSES',
    'cancel_outbound_message',
    'cancel_proactive_commitment',
    'cancel_reminder',
    'claim_content_invitation_for_send',
    'claim_due_proactive_account_state',
    'claim_due_proactive_commitment',
    'claim_due_reminder',
    'claim_reminder_content_run',
    'claim_pending_outbound_by_node',
    'claim_pending_outbound_message',
    'count_active_dynamic_reminders_for_account',
    'count_outbound_in_window',
    'count_total_proactive_outbound_for_quota_date',
    'create_content_invitation',
    'create_outbound_message',
    'create_proactive_commitment',
    'create_reminder',
    'create_reminder_content_run',
    'expire_content_invitations',
    'get_active_content_invitation',
    'get_content_invitation',
    'get_content_invitation_preference',
    'get_outbound_daily_usage',
    'get_outbound_message',
    'get_pending_companion_followup_count_in_window',
    'get_pending_reminder_count_in_window',
    'get_proactive_account_state',
    'get_proactive_commitment',
    'get_proactive_message_settings_row',
    'get_reminder',
    'get_reminder_content_run',
    'insert_global_candidate',
    'insert_proactive_message_setting_event',
    'list_active_global_candidates',
    'list_content_invitations_for_account',
    'list_recent_global_candidate_keys',
    'list_due_proactive_account_states',
    'list_due_proactive_commitments',
    'list_due_reactivation_candidate_accounts',
    'list_due_reminders',
    'list_enqueued_reminder_content_runs',
    'list_outbound_messages',
    'list_proactive_commitments_for_account',
    'list_proactive_message_setting_events',
    'list_reminder_content_runs_for_account',
    'list_reminders_for_account',
    'mark_content_invitation_feedback',
    'mark_content_invitation_invited',
    'mark_content_invitation_rejected_by_policy',
    'mark_content_invitation_titles_sent',
    'mark_outbound_message_failed',
    'mark_outbound_message_sent',
    'mark_proactive_commitment_failed',
    'mark_proactive_commitment_sent',
    'mark_reminder_content_run_enqueued',
    'mark_reminder_content_run_failed',
    'mark_reminder_content_run_sent',
    'mark_reminder_content_run_skipped',
    'mark_reminder_failed',
    'mark_reminder_sent',
    'release_content_invitation_claim',
    'reschedule_reminder_stale_touch',
    'update_outbound_message_metadata',
    'update_reminder',
    'update_reminder_content_meta',
    'upsert_content_invitation_preference',
    'upsert_proactive_account_state',
    'upsert_proactive_message_settings_row',
]
# ---------------------------------------------------------------------------
# Proactive outbound messages
# ---------------------------------------------------------------------------

OUTBOUND_QUOTA_STATUSES = ("pending", "sending", "sent", "failed")


def _decode_outbound_message(row: Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_outbound_message(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    quota_date: str,
    status: str = "pending",
    error: Optional[str] = None,
    product_category: Optional[str] = None,
    policy_version: Optional[str] = None,
    policy_reason: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    node_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_channel = _clean_text(channel)
    cleaned_to_user_id = _clean_text(to_user_id)
    cleaned_source = _clean_text(source)
    cleaned_text = _clean_text(text)
    cleaned_quota_date = _clean_text(quota_date)
    cleaned_status = _clean_text(status) or "pending"
    cleaned_idempotency_key = _clean_text(idempotency_key) or _new_id("out")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_channel:
        raise ValueError("channel is required")
    if not cleaned_to_user_id:
        raise ValueError("to_user_id is required")
    if not cleaned_source:
        raise ValueError("source is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_quota_date:
        raise ValueError("quota_date is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbound_messages(
                account_id, channel, channel_account_id, to_user_id, session_key,
                source, text, idempotency_key, status, error, quota_date,
                product_category, policy_version, policy_reason, scheduled_at,
                node_id, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                cleaned_account_id,
                cleaned_channel,
                _clean_text(channel_account_id),
                cleaned_to_user_id,
                _clean_text(session_key),
                cleaned_source,
                cleaned_text,
                cleaned_idempotency_key,
                cleaned_status,
                error,
                cleaned_quota_date,
                _clean_text(product_category),
                _clean_text(policy_version),
                _clean_text(policy_reason),
                _clean_text(scheduled_at),
                _clean_text(node_id),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM outbound_messages
            WHERE idempotency_key = ?
            """,
            (cleaned_idempotency_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("outbound_message was not created")
    return _decode_outbound_message(row)


def get_outbound_message(*, outbound_message_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def update_outbound_message_metadata(
    *,
    outbound_message_id: int,
    metadata_patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Merge metadata into one outbound message row."""

    current = get_outbound_message(outbound_message_id=outbound_message_id)
    if current is None:
        return None
    metadata = {
        **(current.get("metadata") or {}),
        **(metadata_patch or {}),
    }
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET metadata_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False), outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def list_outbound_messages(
    *,
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM outbound_messages
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_outbound_message(row) for row in rows]


def get_outbound_daily_usage(
    *,
    account_id: str,
    quota_date: str,
    product_category: Optional[str] = None,
) -> int:
    placeholders = ", ".join("?" for _ in OUTBOUND_QUOTA_STATUSES)
    category_clause = ""
    params: List[Any] = [account_id, quota_date, *OUTBOUND_QUOTA_STATUSES]
    if product_category:
        category_clause = " AND product_category = ?"
        params.append(product_category)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ({placeholders})
              {category_clause}
            """,
            params,
        ).fetchone()
    return int(row["count"]) if row else 0


def count_outbound_in_window(
    *,
    account_id: str,
    product_category: str,
    since: str,
) -> int:
    """Count outbound rows of one category since *since* (rolling window).

    Uses created_at (北京时间字符串) >= since, mirroring get_outbound_daily_usage
    status set. For per-category weekly frequency caps.
    """
    placeholders = ", ".join("?" for _ in OUTBOUND_QUOTA_STATUSES)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND created_at >= ?
              AND status IN ({placeholders})
              AND product_category = ?
            """,
            (account_id, since, *OUTBOUND_QUOTA_STATUSES, product_category),
        ).fetchone()
    return int(row["count"]) if row else 0


_PROACTIVE_EXEMPT_CATEGORIES = tuple(cat.value for cat in EXEMPT_CATEGORIES)


def count_total_proactive_outbound_for_quota_date(
    *,
    account_id: str,
    quota_date: str,
) -> int:
    """Count all non-exempt proactive outbound messages for a given quota_date.

    Excludes user_reminder / content_invitation_response / task_result which are
    fully exempt from frequency controls. Used for the global total_per_day cap.
    """
    exempt_placeholders = ", ".join("?" for _ in _PROACTIVE_EXEMPT_CATEGORIES)
    status_placeholders = ", ".join("?" for _ in OUTBOUND_QUOTA_STATUSES)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM outbound_messages
            WHERE account_id = ?
              AND quota_date = ?
              AND status IN ({status_placeholders})
              AND product_category NOT IN ({exempt_placeholders})
            """,
            (
                account_id,
                quota_date,
                *OUTBOUND_QUOTA_STATUSES,
                *_PROACTIVE_EXEMPT_CATEGORIES,
            ),
        ).fetchone()
    return int(row["count"]) if row else 0


def get_pending_reminder_count_in_window(
    *,
    account_id: str,
    start_at: str,
    end_at: str,
) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM reminders
            WHERE account_id = ?
              AND status IN ('pending', 'sending')
              AND due_at >= ?
              AND due_at < ?
            """,
            (account_id, start_at, end_at),
        ).fetchone()
    return int(row["count"]) if row else 0


def get_pending_companion_followup_count_in_window(
    *,
    account_id: str,
    start_at: str,
    end_at: str,
) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM proactive_commitments
                    WHERE account_id = ?
                      AND status IN ('pending', 'sending')
                      AND due_at >= ?
                      AND due_at < ?
                )
                +
                (
                    SELECT COUNT(*)
                    FROM outbound_messages
                    WHERE account_id = ?
                      AND product_category = 'companion_followup'
                      AND status IN ('pending', 'sending', 'sent')
                      AND created_at >= ?
                      AND created_at < ?
                ) AS count
            """,
            (account_id, start_at, end_at, account_id, start_at, end_at),
        ).fetchone()
    return int(row["count"]) if row else 0


def claim_pending_outbound_message(
    *,
    outbound_message_id: int,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'sending',
                attempts = attempts + 1,
                error = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'pending'
            """,
            (outbound_message_id,),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def claim_pending_outbound_by_node(
    *,
    node_id: str,
    batch_size: int,
    claim_timeout_seconds: int,
    max_attempts: int = 5,
) -> List[Dict[str, Any]]:
    """认领某节点的待发主动消息(多机出站,见 multi_node_access_refactor.md 附录 B.3)。

    候选 = status='pending',或卡在 'sending' 且认领超时(claimed_at 早于 stale 阈值,
    节点崩溃回收)。每行用「带条件 UPDATE + rowcount==1」原子抢占,保证同 node 多消费者
    不重复领;WHERE node_id=? 使不同节点天然互斥。仅领 attempts<max_attempts(毒消息封顶)、
    scheduled_at 已到点的行。照搬 claim_queued_content_moderation_tasks 抢占范式。
    """
    cleaned_node_id = _clean_text(node_id)
    if not cleaned_node_id:
        return []
    batch = max(1, min(200, int(batch_size)))
    stale_modifier = f"-{max(1, int(claim_timeout_seconds))} seconds"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM outbound_messages
            WHERE node_id = ?
              AND attempts < ?
              AND (
                status = 'pending'
                OR (
                  status = 'sending'
                  AND claimed_at IS NOT NULL
                  AND claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?))
                )
              )
              AND (
                scheduled_at IS NULL
                OR scheduled_at <= strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
              )
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (cleaned_node_id, int(max_attempts), stale_modifier, batch),
        ).fetchall()
        claimed: List[Dict[str, Any]] = []
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'sending',
                    attempts = attempts + 1,
                    claimed_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    error = NULL,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                  AND node_id = ?
                  AND attempts < ?
                  AND (
                    status = 'pending'
                    OR (
                      status = 'sending'
                      AND claimed_at IS NOT NULL
                      AND claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?))
                    )
                  )
                """,
                (row["id"], cleaned_node_id, int(max_attempts), stale_modifier),
            )
            if cursor.rowcount != 1:
                continue
            claimed_row = conn.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if claimed_row is not None:
                claimed.append(_decode_outbound_message(claimed_row))
    return claimed


def mark_outbound_message_sent(
    *,
    outbound_message_id: int,
    gateway_message_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'sent',
                gateway_message_id = ?,
                error = NULL,
                sent_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (gateway_message_id, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def mark_outbound_message_failed(
    *,
    outbound_message_id: int,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'failed',
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (error, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


def cancel_outbound_message(
    *,
    outbound_message_id: int,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = 'cancelled',
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (error, outbound_message_id),
        )
        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (outbound_message_id,),
        ).fetchone()
    return _decode_outbound_message(row) if row else None


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def _decode_reminder(row: Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    # dynamic 提醒的履约参数（旧行无此列/为空 → 视作 fixed）。
    content_meta_json = item.pop("content_meta_json", None)
    try:
        item["content_meta"] = json.loads(content_meta_json or "{}")
    except (json.JSONDecodeError, TypeError):
        item["content_meta"] = {}
        item["content_meta_decode_error"] = True
    if not item.get("fulfillment"):
        item["fulfillment"] = "fixed"
    return item


def create_reminder(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    text: str,
    due_at: str,
    reminder_id: Optional[str] = None,
    recur_rule: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    fulfillment: str = "fixed",
    content_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_channel = _clean_text(channel)
    cleaned_to_user_id = _clean_text(to_user_id)
    cleaned_text = _clean_text(text)
    cleaned_due_at = _clean_text(due_at)
    cleaned_reminder_id = _clean_text(reminder_id) or _new_id("rem")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_channel:
        raise ValueError("channel is required")
    if not cleaned_to_user_id:
        raise ValueError("to_user_id is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_due_at:
        raise ValueError("due_at is required")

    cleaned_fulfillment = _clean_text(fulfillment) or "fixed"
    if cleaned_fulfillment not in ("fixed", "dynamic"):
        raise ValueError(f"invalid fulfillment: {fulfillment!r}")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reminders(
                id, account_id, channel, channel_account_id, to_user_id,
                session_key, text, due_at, recur_rule, metadata_json,
                fulfillment, content_meta_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                cleaned_reminder_id,
                cleaned_account_id,
                cleaned_channel,
                _clean_text(channel_account_id),
                cleaned_to_user_id,
                _clean_text(session_key),
                cleaned_text,
                cleaned_due_at,
                _clean_text(recur_rule) if recur_rule else None,
                json.dumps(metadata or {}, ensure_ascii=False),
                cleaned_fulfillment,
                json.dumps(content_meta, ensure_ascii=False) if content_meta else None,
            ),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (cleaned_reminder_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("reminder was not created")
    return _decode_reminder(row)


def get_reminder(*, reminder_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def list_due_reminders(
    *,
    now: str,
    limit: int = 20,
    node_id: Optional[str] = None,
    fulfillment: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """到期提醒扫描。

    - node_id 非空时只返回归属该节点的账号的提醒（厚节点改造 P4 调度分片）。
    - fulfillment 非空时按履约方式过滤：'fixed' 供现有固定提醒调度（节点分片）；
      'dynamic' 供动态提醒调度（统一 aliyun1 调度，node_id 传 None 跨节点覆盖远程账号）。
      不传则不过滤（保持历史行为）。
    """
    node_filter = _clean_text(node_id) if node_id else None
    fulfillment_filter = _clean_text(fulfillment) if fulfillment else None
    conditions = ["r.status = 'pending'", "r.due_at <= ?", "a.app_id = ?"]
    params: List[Any] = [now, ZHAOXI_APP_ID]
    join_clause = "JOIN accounts a ON a.id = r.account_id"
    if node_filter:
        conditions.append("a.assigned_node_id = ?")
        params.append(node_filter)
    if fulfillment_filter:
        # 旧行 fulfillment 为空视作 fixed，用 COALESCE 覆盖历史数据。
        conditions.append("COALESCE(r.fulfillment, 'fixed') = ?")
        params.append(fulfillment_filter)
    params.append(limit)
    sql = f"""
        SELECT r.*
        FROM reminders r
        {join_clause}
        WHERE {' AND '.join(conditions)}
        ORDER BY r.due_at ASC, r.created_at ASC
        LIMIT ?
    """
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_decode_reminder(row) for row in rows]


def list_reminders_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE account_id = ? AND status = ?
                ORDER BY due_at ASC, created_at ASC
                LIMIT ?
                """,
                (account_id, status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE account_id = ?
                ORDER BY due_at DESC, created_at DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
    return [_decode_reminder(row) for row in rows]


def claim_due_reminder(*, reminder_id: str, now: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'sending',
                attempts = attempts + 1,
                claimed_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                error = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'pending'
              AND due_at <= ?
              AND EXISTS (
                  SELECT 1 FROM accounts
                  WHERE accounts.id = reminders.account_id
                    AND accounts.status = 'active'
                    AND accounts.app_id = ?
              )
            """,
            (reminder_id, now, ZHAOXI_APP_ID),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def mark_reminder_sent(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    next_due_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        if next_due_at:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'pending',
                    due_at = ?,
                    sent_count = sent_count + 1,
                    last_sent_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    claimed_at = NULL,
                    outbound_message_id = ?,
                    error = NULL,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (next_due_at, outbound_message_id, reminder_id),
            )
        else:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'sent',
                    sent_count = sent_count + 1,
                    last_sent_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    outbound_message_id = ?,
                    error = NULL,
                    sent_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (outbound_message_id, reminder_id),
            )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def reschedule_reminder_stale_touch(
    *,
    reminder_id: str,
    next_due_at: str,
    error: str,
) -> Optional[Dict[str, Any]]:
    """周期提醒因微信送达窗口过期跳过本次触发：不计入 sent_count/last_sent_at（没有真的发送），
    只把 due_at 推进到下一周期，序列继续（见 docs/plans/主动消息送达窗口对齐.md）。
    """
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'pending',
                due_at = ?,
                claimed_at = NULL,
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (next_due_at, error, reminder_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def mark_reminder_failed(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'failed',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, error, reminder_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def cancel_reminder(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                cancelled_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, error, reminder_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    return _decode_reminder(row) if row else None


def update_reminder(
    *,
    reminder_id: str,
    text: Optional[str] = None,
    due_at: Optional[str] = None,
    recur_rule: Optional[str] = None,
    clear_recur_rule: bool = False,
) -> Optional[Dict[str, Any]]:
    fields: List[str] = []
    values: List = []
    if text is not None:
        fields.append("text = ?")
        values.append(_clean_text(text))
    if due_at is not None:
        fields.append("due_at = ?")
        values.append(_clean_text(due_at))
    if recur_rule is not None:
        fields.append("recur_rule = ?")
        values.append(_clean_text(recur_rule))
    elif clear_recur_rule:
        fields.append("recur_rule = NULL")
    if not fields:
        return get_reminder(reminder_id=reminder_id)
    fields.append("updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))")
    values.append(reminder_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return _decode_reminder(row) if row else None


def count_active_dynamic_reminders_for_account(*, account_id: str) -> int:
    """账号当前活跃（status='pending'）的动态提醒数，供创建时的每账号上限校验。"""
    cleaned = _clean_text(account_id)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM reminders
            WHERE account_id = ?
              AND status = 'pending'
              AND COALESCE(fulfillment, 'fixed') = 'dynamic'
            """,
            (cleaned,),
        ).fetchone()
    return int((dict(row) if row else {}).get("n", 0) or 0)


# ---------------------------------------------------------------------------
# Dynamic reminder content runs（每次动态履约一行；见动态提醒设计文档）
# ---------------------------------------------------------------------------

def _decode_reminder_content_run(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for src, dst, default in (
        ("search_trace_json", "search_trace", None),
        ("metadata_json", "metadata", {}),
    ):
        raw = item.pop(src, None)
        try:
            item[dst] = json.loads(raw) if raw else default
        except (json.JSONDecodeError, TypeError):
            item[dst] = default
            item[f"{dst}_decode_error"] = True
    return item


def create_reminder_content_run(
    *,
    reminder_id: str,
    account_id: str,
    scheduled_for: str,
    run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """为一次到期履约建 run 行。

    UNIQUE(reminder_id, scheduled_for) 保证同一周期只建一次：若该周期已存在（重复扫描/
    重复认领）返回 None，调用方据此跳过，杜绝重复搜索/重复发送。
    """
    cleaned_reminder_id = _clean_text(reminder_id)
    cleaned_account_id = _clean_text(account_id)
    cleaned_scheduled_for = _clean_text(scheduled_for)
    cleaned_run_id = _clean_text(run_id) or _new_id("rcr")
    if not cleaned_reminder_id or not cleaned_account_id or not cleaned_scheduled_for:
        raise ValueError("reminder_id/account_id/scheduled_for are required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO reminder_content_runs(
                id, reminder_id, account_id, scheduled_for, status,
                attempts, started_at, created_at
            )
            VALUES (?, ?, ?, ?, 'running', 1,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (cleaned_run_id, cleaned_reminder_id, cleaned_account_id, cleaned_scheduled_for),
        )
        if cursor.rowcount != 1:
            # 该 (reminder_id, scheduled_for) 已存在 → 本周期已被处理，跳过。
            return None
        row = conn.execute(
            "SELECT * FROM reminder_content_runs WHERE id = ?",
            (cleaned_run_id,),
        ).fetchone()
    return _decode_reminder_content_run(row) if row else None


def claim_reminder_content_run(
    *,
    reminder_id: str,
    account_id: str,
    scheduled_for: str,
    max_attempts: int = 1,
    run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """认领某周期的履约 run（支持同周期有限次重试）。

    - 该 (reminder_id, scheduled_for) 尚无 run → 新建 running(attempts=1) 并返回。
    - 已存在但为可重试态（failed/running）且 attempts < max_attempts → 复用：置 running、
      attempts+1 并返回（同周期重试，不产生新行，天然不重复占用）。
    - 已存在且为终态（sent/enqueued/skipped）或重试已耗尽 → 返回 None（调用方据此跳过并推进周期）。

    UNIQUE(reminder_id, scheduled_for) 保证「同周期只有一行」，据此杜绝重复搜索/发送。
    """
    cleaned_reminder_id = _clean_text(reminder_id)
    cleaned_account_id = _clean_text(account_id)
    cleaned_scheduled_for = _clean_text(scheduled_for)
    cleaned_run_id = _clean_text(run_id) or _new_id("rcr")
    if not cleaned_reminder_id or not cleaned_account_id or not cleaned_scheduled_for:
        raise ValueError("reminder_id/account_id/scheduled_for are required")
    max_attempts = max(1, int(max_attempts))
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO reminder_content_runs(
                id, reminder_id, account_id, scheduled_for, status,
                attempts, started_at, created_at
            )
            VALUES (?, ?, ?, ?, 'running', 1,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (cleaned_run_id, cleaned_reminder_id, cleaned_account_id, cleaned_scheduled_for),
        )
        if cursor.rowcount == 1:
            row = conn.execute(
                "SELECT * FROM reminder_content_runs WHERE id = ?", (cleaned_run_id,)
            ).fetchone()
            return _decode_reminder_content_run(row) if row else None
        # 已存在：只有可重试态且未耗尽才复用。
        existing = conn.execute(
            """
            SELECT * FROM reminder_content_runs
            WHERE reminder_id = ? AND scheduled_for = ?
            """,
            (cleaned_reminder_id, cleaned_scheduled_for),
        ).fetchone()
        if existing is None:
            return None
        run = _decode_reminder_content_run(existing)
        if run.get("status") not in ("failed", "running"):
            return None
        if int(run.get("attempts") or 0) >= max_attempts:
            return None
        conn.execute(
            """
            UPDATE reminder_content_runs
            SET status = 'running',
                attempts = attempts + 1,
                error = NULL,
                started_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (run["id"],),
        )
        row = conn.execute(
            "SELECT * FROM reminder_content_runs WHERE id = ?", (run["id"],)
        ).fetchone()
    return _decode_reminder_content_run(row) if row else None


def update_reminder_content_meta(
    *, reminder_id: str, patch: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """合并式更新提醒的 content_meta_json（读改写，供记录 last_success_run_at 等）。"""
    cleaned = _clean_text(reminder_id)
    with connect() as conn:
        row = conn.execute(
            "SELECT content_meta_json FROM reminders WHERE id = ?", (cleaned,)
        ).fetchone()
        if row is None:
            return None
        current_raw = (dict(row) if row else {}).get("content_meta_json")
        try:
            current = json.loads(current_raw) if current_raw else {}
        except (json.JSONDecodeError, TypeError):
            current = {}
        current.update(patch or {})
        conn.execute(
            """
            UPDATE reminders
            SET content_meta_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (json.dumps(current, ensure_ascii=False), cleaned),
        )
        updated = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (cleaned,)
        ).fetchone()
    return _decode_reminder(updated) if updated else None


def _finish_reminder_content_run(
    *,
    run_id: str,
    status: str,
    outbound_message_id: Optional[int] = None,
    generated_text: Optional[str] = None,
    search_ok: Optional[bool] = None,
    search_trace: Optional[Any] = None,
    error: Optional[str] = None,
    finished: bool = True,
) -> Optional[Dict[str, Any]]:
    """统一的 run 终态/中间态写入。finished=False 用于 enqueued（尚未终结，待对账）。"""
    fields = ["status = ?"]
    values: List[Any] = [status]
    if outbound_message_id is not None:
        fields.append("outbound_message_id = ?")
        values.append(outbound_message_id)
    if generated_text is not None:
        fields.append("generated_text = ?")
        values.append(generated_text)
    if search_ok is not None:
        fields.append("search_ok = ?")
        values.append(1 if search_ok else 0)
    if search_trace is not None:
        fields.append("search_trace_json = ?")
        values.append(json.dumps(search_trace, ensure_ascii=False))
    if error is not None:
        fields.append("error = ?")
        values.append(error)
    if finished:
        fields.append(
            "finished_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))"
        )
    values.append(run_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE reminder_content_runs SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        row = conn.execute(
            "SELECT * FROM reminder_content_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _decode_reminder_content_run(row) if row else None


def mark_reminder_content_run_sent(
    *,
    run_id: str,
    outbound_message_id: Optional[int] = None,
    generated_text: Optional[str] = None,
    search_ok: Optional[bool] = None,
    search_trace: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    return _finish_reminder_content_run(
        run_id=run_id,
        status="sent",
        outbound_message_id=outbound_message_id,
        generated_text=generated_text,
        search_ok=search_ok,
        search_trace=search_trace,
    )


def mark_reminder_content_run_enqueued(
    *,
    run_id: str,
    outbound_message_id: Optional[int] = None,
    generated_text: Optional[str] = None,
    search_ok: Optional[bool] = None,
    search_trace: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """远程账号出站入队（尚未确认发送）：不写 finished_at，等对账翻 sent/failed。"""
    return _finish_reminder_content_run(
        run_id=run_id,
        status="enqueued",
        outbound_message_id=outbound_message_id,
        generated_text=generated_text,
        search_ok=search_ok,
        search_trace=search_trace,
        finished=False,
    )


def mark_reminder_content_run_skipped(
    *, run_id: str, reason: str
) -> Optional[Dict[str, Any]]:
    return _finish_reminder_content_run(run_id=run_id, status="skipped", error=reason)


def mark_reminder_content_run_failed(
    *, run_id: str, error: str, outbound_message_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    return _finish_reminder_content_run(
        run_id=run_id,
        status="failed",
        error=error,
        outbound_message_id=outbound_message_id,
    )


def get_reminder_content_run(*, run_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminder_content_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _decode_reminder_content_run(row) if row else None


def list_enqueued_reminder_content_runs(*, limit: int = 100) -> List[Dict[str, Any]]:
    """待对账的 run（远程出站已入队、尚未确认终态），按创建时间升序。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reminder_content_runs
            WHERE status = 'enqueued'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_decode_reminder_content_run(row) for row in rows]


def list_reminder_content_runs_for_account(
    *, account_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """账号级 run 历史（供 admin/debug 查询），强制按 account_id 隔离。"""
    cleaned = _clean_text(account_id)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM reminder_content_runs
            WHERE account_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cleaned, limit),
        ).fetchall()
    return [_decode_reminder_content_run(row) for row in rows]


# ---------------------------------------------------------------------------
# Proactive commitments
# ---------------------------------------------------------------------------

def _decode_proactive_commitment(row: Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_proactive_commitment(
    *,
    account_id: str,
    text: str,
    due_at: str,
    session_id: Optional[int] = None,
    source_message_id: Optional[str] = None,
    source_reply_message_id: Optional[str] = None,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    commitment_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_text = _clean_text(text)
    cleaned_due_at = _clean_text(due_at)
    cleaned_commitment_id = _clean_text(commitment_id) or _new_id("com")
    cleaned_dedupe_key = _clean_text(dedupe_key) or cleaned_commitment_id
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_text:
        raise ValueError("text is required")
    if not cleaned_due_at:
        raise ValueError("due_at is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO proactive_commitments(
                id, account_id, session_id, source_message_id, source_reply_message_id,
                dedupe_key, text, due_at, confidence, reason, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                cleaned_commitment_id,
                cleaned_account_id,
                session_id,
                _clean_text(source_message_id),
                _clean_text(source_reply_message_id),
                cleaned_dedupe_key,
                cleaned_text,
                cleaned_due_at,
                confidence,
                _clean_text(reason),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE dedupe_key = ?",
            (cleaned_dedupe_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("proactive_commitment was not created")
    return _decode_proactive_commitment(row)


def get_proactive_commitment(*, commitment_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def list_proactive_commitments_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = ["account_id = ?"]
    params: List[Any] = [account_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM proactive_commitments
            WHERE {' AND '.join(clauses)}
            ORDER BY due_at DESC, created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_proactive_commitment(row) for row in rows]


def list_due_proactive_commitments(
    *,
    now: str,
    limit: int = 20,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """node_id 非空时只返回归属该节点的账号的承诺（厚节点改造 P4 调度分片）。"""
    node_filter = _clean_text(node_id) if node_id else None
    node_clause = "AND a.assigned_node_id = ?" if node_filter else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*
            FROM proactive_commitments c
            JOIN accounts a ON a.id = c.account_id
            JOIN proactive_account_state s ON s.account_id = c.account_id
            WHERE c.status = 'pending'
              AND c.due_at <= ?
              AND a.status = 'active'
              AND a.app_id = ?
              AND s.enabled = 1
              AND (s.cooldown_until IS NULL OR s.cooldown_until <= ?)
              {node_clause}
            ORDER BY c.due_at ASC, c.created_at ASC
            LIMIT ?
            """,
            [now, ZHAOXI_APP_ID, now] + ([node_filter] if node_filter else []) + [limit],
        ).fetchall()
    return [_decode_proactive_commitment(row) for row in rows]


def claim_due_proactive_commitment(
    *,
    commitment_id: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    cleaned_commitment_id = _clean_text(commitment_id)
    cleaned_now = _clean_text(now)
    if not cleaned_commitment_id:
        raise ValueError("commitment_id is required")
    if not cleaned_now:
        raise ValueError("now is required")

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'sending',
                attempts = attempts + 1,
                claimed_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                error = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'pending'
              AND due_at <= ?
              AND EXISTS (
                  SELECT 1 FROM accounts
                  WHERE accounts.id = proactive_commitments.account_id
                    AND accounts.status = 'active'
                    AND accounts.app_id = ?
              )
              AND EXISTS (
                  SELECT 1 FROM proactive_account_state
                  WHERE proactive_account_state.account_id = proactive_commitments.account_id
                    AND proactive_account_state.enabled = 1
                    AND (
                        proactive_account_state.cooldown_until IS NULL
                        OR proactive_account_state.cooldown_until <= ?
                    )
              )
            """,
            (cleaned_commitment_id, cleaned_now, ZHAOXI_APP_ID, cleaned_now),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (cleaned_commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def mark_proactive_commitment_sent(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'sent',
                outbound_message_id = ?,
                error = NULL,
                sent_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def mark_proactive_commitment_failed(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int] = None,
    error: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'failed',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, error, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


def cancel_proactive_commitment(
    *,
    commitment_id: str,
    outbound_message_id: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_commitments
            SET status = 'cancelled',
                outbound_message_id = COALESCE(?, outbound_message_id),
                error = ?,
                cancelled_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, error, commitment_id),
        )
        row = conn.execute(
            "SELECT * FROM proactive_commitments WHERE id = ?",
            (commitment_id,),
        ).fetchone()
    return _decode_proactive_commitment(row) if row else None


# ---------------------------------------------------------------------------
# Proactive account state
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Global (ownerless) candidate pool —— proactive_global_candidates
# 见 _migration_0011：刻意无 account_id（隔离不变量的显式例外）。
# ---------------------------------------------------------------------------

def _decode_global_candidate(row: Row) -> Dict[str, Any]:
    item = dict(row)
    raw = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        item["metadata"] = {}
    return item


def insert_global_candidate(
    *,
    kind: str,
    topic: Optional[str],
    text: str,
    generated_date: str,
    dedupe_key: str,
    expires_at: str,
    created_at: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """入池一条全局候选；命中 UNIQUE(kind,generated_date,dedupe_key) 则静默忽略（幂等）。

    调用方在入池前已做历史去重，这里的 INSERT OR IGNORE 只作并发/重跑的兜底防重，
    不返回是否命中（跨后端 rowcount 语义不统一，调用方按"尝试集 - 已存在集"自行计数）。
    """
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO proactive_global_candidates(
                kind, topic, text, generated_date, dedupe_key, expires_at, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                topic,
                text,
                generated_date,
                dedupe_key,
                expires_at,
                json.dumps(metadata or {}, ensure_ascii=False),
                created_at,
            ),
        )


def list_active_global_candidates(
    *,
    kind: str,
    now: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """未过期（expires_at > now）的某类全局候选，新到旧。now 为北京 naive 时间字符串。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM proactive_global_candidates
            WHERE kind = ? AND expires_at > ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (kind, now, max(int(limit), 1)),
        ).fetchall()
    return [_decode_global_candidate(row) for row in rows]


def list_recent_global_candidate_keys(
    *,
    kind: str,
    since_date: str,
) -> List[str]:
    """generated_date >= since_date 的已入池 dedupe_key 去重列表，供生成时历史去重。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT dedupe_key FROM proactive_global_candidates
            WHERE kind = ? AND generated_date >= ?
            """,
            (kind, since_date),
        ).fetchall()
    return [row["dedupe_key"] for row in rows]


def _decode_proactive_account_state(row: Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    item["enabled"] = bool(item.get("enabled"))
    return item


def get_proactive_account_state(*, account_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_account_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return _decode_proactive_account_state(row) if row else None


def upsert_proactive_account_state(
    *,
    account_id: str,
    enabled=_UNSET,
    next_scan_at=_UNSET,
    last_scan_at=_UNSET,
    last_proactive_sent_at=_UNSET,
    cooldown_until=_UNSET,
    metadata=_UNSET,
    metadata_patch=_UNSET,
) -> Dict[str, Any]:
    """Upsert proactive_account_state for an account.

    metadata replaces the entire metadata JSON (legacy semantics).
    metadata_patch applies an RFC 7396 JSON Merge Patch via SQLite json_patch
    in a single statement: only the named keys are written, and keys mapped to
    None are removed. metadata_patch is preferred for concurrent updaters
    (scheduler + admin) because it avoids the read-modify-write race that
    silently drops sibling keys; metadata and metadata_patch are mutually
    exclusive.
    """
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if metadata is not _UNSET and metadata_patch is not _UNSET:
        raise ValueError("metadata and metadata_patch are mutually exclusive")

    current = get_proactive_account_state(account_id=cleaned_account_id)
    if current is None:
        enabled_value = 1 if enabled is _UNSET else int(bool(enabled))
        next_scan_at_value = None if next_scan_at is _UNSET else _clean_text(next_scan_at)
        last_scan_at_value = None if last_scan_at is _UNSET else _clean_text(last_scan_at)
        last_proactive_sent_at_value = (
            None
            if last_proactive_sent_at is _UNSET
            else _clean_text(last_proactive_sent_at)
        )
        cooldown_until_value = (
            None if cooldown_until is _UNSET else _clean_text(cooldown_until)
        )
        if metadata_patch is not _UNSET:
            # Patch over {} just drops null keys; equivalent to a normal insert
            # with the non-null keys.
            seed: Dict[str, Any] = {
                key: value
                for key, value in (metadata_patch or {}).items()
                if value is not None
            }
            metadata_value = seed
        elif metadata is _UNSET:
            metadata_value = {}
        else:
            metadata_value = metadata or {}
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO proactive_account_state(
                    account_id, enabled, next_scan_at, last_scan_at,
                    last_proactive_sent_at, cooldown_until, metadata_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (
                    cleaned_account_id,
                    enabled_value,
                    next_scan_at_value,
                    last_scan_at_value,
                    last_proactive_sent_at_value,
                    cooldown_until_value,
                    json.dumps(metadata_value, ensure_ascii=False),
                ),
            )
    else:
        if metadata_patch is not _UNSET:
            # Atomic merge: json_patch in a single UPDATE means concurrent
            # callers cannot silently drop each other's sibling keys.
            patch_json = json.dumps(metadata_patch or {}, ensure_ascii=False)
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE proactive_account_state
                    SET enabled = ?,
                        next_scan_at = ?,
                        last_scan_at = ?,
                        last_proactive_sent_at = ?,
                        cooldown_until = ?,
                        metadata_json = json_patch(
                            COALESCE(metadata_json, '{}'),
                            ?
                        ),
                        updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                    WHERE account_id = ?
                    """,
                    (
                        int(current["enabled"] if enabled is _UNSET else bool(enabled)),
                        current.get("next_scan_at")
                        if next_scan_at is _UNSET
                        else _clean_text(next_scan_at),
                        current.get("last_scan_at")
                        if last_scan_at is _UNSET
                        else _clean_text(last_scan_at),
                        current.get("last_proactive_sent_at")
                        if last_proactive_sent_at is _UNSET
                        else _clean_text(last_proactive_sent_at),
                        current.get("cooldown_until")
                        if cooldown_until is _UNSET
                        else _clean_text(cooldown_until),
                        patch_json,
                        cleaned_account_id,
                    ),
                )
        else:
            next_metadata = current.get("metadata") or {}
            if metadata is not _UNSET:
                next_metadata = metadata or {}
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE proactive_account_state
                    SET enabled = ?,
                        next_scan_at = ?,
                        last_scan_at = ?,
                        last_proactive_sent_at = ?,
                        cooldown_until = ?,
                        metadata_json = ?,
                        updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                    WHERE account_id = ?
                    """,
                    (
                        int(current["enabled"] if enabled is _UNSET else bool(enabled)),
                        current.get("next_scan_at")
                        if next_scan_at is _UNSET
                        else _clean_text(next_scan_at),
                        current.get("last_scan_at")
                        if last_scan_at is _UNSET
                        else _clean_text(last_scan_at),
                        current.get("last_proactive_sent_at")
                        if last_proactive_sent_at is _UNSET
                        else _clean_text(last_proactive_sent_at),
                        current.get("cooldown_until")
                        if cooldown_until is _UNSET
                        else _clean_text(cooldown_until),
                        json.dumps(next_metadata, ensure_ascii=False),
                        cleaned_account_id,
                    ),
                )

    item = get_proactive_account_state(account_id=cleaned_account_id)
    if item is None:
        raise RuntimeError("proactive_account_state was not created")
    return item


# ---------------------------------------------------------------------------
# Proactive message settings（账号级主动消息偏好 + 审计）
# ---------------------------------------------------------------------------

def _decode_proactive_message_settings(row: Row) -> Dict[str, Any]:
    """Decode a proactive_message_settings row.

    quiet_hours 为 None 表示该列为 NULL（继承全局），调用方据此区分
    "未设置"与"用户显式设置"，不要在这里塞默认值。
    """
    item = dict(row)
    for src, dst in (
        ("quiet_hours_json", "quiet_hours"),
        ("category_settings_json", "category_settings"),
        ("frequency_json", "frequency"),
        ("allowed_windows_json", "allowed_windows"),
        ("metadata_json", "metadata"),
    ):
        raw = item.pop(src, None)
        if raw is None:
            # 可空列保持 None；NOT NULL 列建表默认非空，理论上不会进这里
            item[dst] = None
            continue
        try:
            item[dst] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            item[dst] = None
            item[f"{dst}_decode_error"] = True
    item["master_enabled"] = bool(item.get("master_enabled"))
    return item


def get_proactive_message_settings_row(*, account_id: str) -> Optional[Dict[str, Any]]:
    """Return the raw account-level proactive message settings row, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_message_settings WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return _decode_proactive_message_settings(row) if row else None


def upsert_proactive_message_settings_row(
    *,
    account_id: str,
    master_enabled=_UNSET,
    quiet_hours=_UNSET,
    category_settings=_UNSET,
    frequency=_UNSET,
    allowed_windows=_UNSET,
    muted_until=_UNSET,
    metadata=_UNSET,
) -> Dict[str, Any]:
    """Upsert sparse proactive message settings for an account.

    _UNSET 表示不改动该字段。对可空 override 列（quiet_hours/muted_until），传入
    None 表示显式清除（写 NULL，回到继承全局）。category_settings/frequency/
    allowed_windows 为 NOT NULL 列，整体替换 JSON（空容器表示无 override）。
    """
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")

    current = get_proactive_message_settings_row(account_id=cleaned_account_id)

    def _resolve_json(value, current_value):
        """None -> 写 NULL；_UNSET -> 保持现状；其它 -> dump。"""
        if value is _UNSET:
            return current_value
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    if current is None:
        master_value = 1 if master_enabled is _UNSET else int(bool(master_enabled))
        quiet_hours_value = None if quiet_hours in (_UNSET, None) else json.dumps(quiet_hours, ensure_ascii=False)
        category_value = (
            "{}" if category_settings in (_UNSET, None) else json.dumps(category_settings, ensure_ascii=False)
        )
        frequency_value = "{}" if frequency in (_UNSET, None) else json.dumps(frequency, ensure_ascii=False)
        allowed_windows_value = (
            "[]" if allowed_windows in (_UNSET, None) else json.dumps(allowed_windows, ensure_ascii=False)
        )
        muted_value = None if muted_until in (_UNSET, None) else _clean_text(muted_until)
        metadata_value = "{}" if metadata in (_UNSET, None) else json.dumps(metadata, ensure_ascii=False)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO proactive_message_settings(
                    account_id, master_enabled, quiet_hours_json,
                    category_settings_json, frequency_json, allowed_windows_json,
                    muted_until, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (
                    cleaned_account_id,
                    master_value,
                    quiet_hours_value,
                    category_value,
                    frequency_value,
                    allowed_windows_value,
                    muted_value,
                    metadata_value,
                ),
            )
    else:
        master_value = (
            int(current["master_enabled"]) if master_enabled is _UNSET else int(bool(master_enabled))
        )
        # 现存行的 JSON 字段已被 decode 成 dict/list/None，需要还原成可比较的"当前 JSON 串"
        def _current_json(key):
            val = current.get(key)
            return None if val is None else json.dumps(val, ensure_ascii=False)

        quiet_hours_value = _resolve_json(quiet_hours, _current_json("quiet_hours"))
        category_value = _resolve_json(category_settings, _current_json("category_settings"))
        if category_value is None:
            category_value = "{}"
        frequency_value = _resolve_json(frequency, _current_json("frequency"))
        if frequency_value is None:
            frequency_value = "{}"
        allowed_windows_value = _resolve_json(allowed_windows, _current_json("allowed_windows"))
        if allowed_windows_value is None:
            allowed_windows_value = "[]"
        muted_value = (
            current.get("muted_until")
            if muted_until is _UNSET
            else (None if muted_until is None else _clean_text(muted_until))
        )
        metadata_value = _resolve_json(metadata, _current_json("metadata"))
        if metadata_value is None:
            metadata_value = "{}"
        with connect() as conn:
            conn.execute(
                """
                UPDATE proactive_message_settings
                SET master_enabled = ?,
                    quiet_hours_json = ?,
                    category_settings_json = ?,
                    frequency_json = ?,
                    allowed_windows_json = ?,
                    muted_until = ?,
                    metadata_json = ?,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE account_id = ?
                """,
                (
                    master_value,
                    quiet_hours_value,
                    category_value,
                    frequency_value,
                    allowed_windows_value,
                    muted_value,
                    metadata_value,
                    cleaned_account_id,
                ),
            )

    item = get_proactive_message_settings_row(account_id=cleaned_account_id)
    if item is None:
        raise RuntimeError("proactive_message_settings was not created")
    return item


def insert_proactive_message_setting_event(
    *,
    account_id: str,
    source: str,
    tool_invocation_id: Optional[int] = None,
    previous_settings: Optional[Dict[str, Any]] = None,
    patch: Dict[str, Any],
    next_settings: Dict[str, Any],
    reason: Optional[str] = None,
) -> int:
    """Record an audit event for a proactive message settings change. Returns row id."""
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO proactive_message_setting_events(
                account_id, source, tool_invocation_id,
                previous_settings_json, patch_json, next_settings_json, reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_account_id,
                source,
                tool_invocation_id,
                None if previous_settings is None else json.dumps(previous_settings, ensure_ascii=False),
                json.dumps(patch, ensure_ascii=False),
                json.dumps(next_settings, ensure_ascii=False),
                _clean_text(reason),
            ),
        )
        return int(cur.lastrowid)


def list_proactive_message_setting_events(
    *,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return recent setting events for an account, newest first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM proactive_message_setting_events
            WHERE account_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def list_due_proactive_account_states(
    *,
    now: str,
    limit: int = 20,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """node_id 非空时只返回归属该节点的账号状态（厚节点改造 P4 调度分片）。"""
    node_filter = _clean_text(node_id) if node_id else None
    node_clause = "AND a.assigned_node_id = ?" if node_filter else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT s.*, a.status AS account_status
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.enabled = 1
              AND a.status = 'active'
              AND a.app_id = ?
              AND (s.next_scan_at IS NULL OR s.next_scan_at <= ?)
              AND (s.cooldown_until IS NULL OR s.cooldown_until <= ?)
              {node_clause}
            ORDER BY COALESCE(s.next_scan_at, '0000-01-01 00:00:00') ASC,
                     s.updated_at ASC
            LIMIT ?
            """,
            [ZHAOXI_APP_ID, now, now]
            + ([node_filter] if node_filter else [])
            + [limit],
        ).fetchall()
    return [_decode_proactive_account_state(row) for row in rows]


def list_due_reactivation_candidate_accounts(
    *, now: str, limit: int = 20, node_id: Optional[str] = None
) -> List[str]:
    """Accounts whose queued reactivation candidate is due to send (scheduled_at<=now).

    node_id 非空时只返回归属该节点的账号（厚节点改造 P4 调度分片）。
    """
    node_filter = _clean_text(node_id) if node_id else None
    node_clause = "AND a.assigned_node_id = ?" if node_filter else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT s.account_id
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.enabled = 1
              AND a.status = 'active'
              AND a.app_id = ?
              {node_clause}
              AND json_valid(s.metadata_json)
              AND json_extract(s.metadata_json, '$.reactivation_candidate.scheduled_at') IS NOT NULL
              AND json_extract(s.metadata_json, '$.reactivation_candidate.scheduled_at') <= ?
            ORDER BY json_extract(s.metadata_json, '$.reactivation_candidate.scheduled_at') ASC,
                     s.updated_at ASC
            LIMIT ?
            """,
            [ZHAOXI_APP_ID]
            + ([node_filter] if node_filter else [])
            + [now, limit],
        ).fetchall()
    return [row["account_id"] for row in rows]


def claim_due_proactive_account_state(
    *,
    account_id: str,
    now: str,
    next_scan_at: str,
) -> Optional[Dict[str, Any]]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_now = _clean_text(now)
    cleaned_next_scan_at = _clean_text(next_scan_at)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_now:
        raise ValueError("now is required")
    if not cleaned_next_scan_at:
        raise ValueError("next_scan_at is required")

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE proactive_account_state
            SET last_scan_at = ?,
                next_scan_at = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ?
              AND enabled = 1
              AND (next_scan_at IS NULL OR next_scan_at <= ?)
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
              AND EXISTS (
                  SELECT 1
                  FROM accounts
                  WHERE accounts.id = proactive_account_state.account_id
                    AND accounts.status = 'active'
                    AND accounts.app_id = ?
              )
            """,
            (
                cleaned_now,
                cleaned_next_scan_at,
                cleaned_account_id,
                cleaned_now,
                cleaned_now,
                ZHAOXI_APP_ID,
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            """
            SELECT s.*, a.status AS account_status
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            WHERE s.account_id = ? AND a.app_id = ?
            """,
            (cleaned_account_id, ZHAOXI_APP_ID),
        ).fetchone()
    return _decode_proactive_account_state(row) if row else None


# ---------------------------------------------------------------------------
# Content invitations
# ---------------------------------------------------------------------------

CONTENT_INVITATION_ACTIVE_STATUSES = ("candidate", "sending", "invited", "accepted")


def _normalize_title_items(title_items: Any) -> List[Dict[str, Any]]:
    if not isinstance(title_items, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in title_items:
        if isinstance(item, dict):
            title = _clean_text(item.get("title"))
            if not title:
                continue
            normalized.append(
                {
                    "title": title[:200],
                    "source_name": _clean_text(item.get("source_name")),
                    "url": _clean_text(item.get("url")),
                    "published_at": _clean_text(item.get("published_at")),
                    "retrieved_at": _clean_text(item.get("retrieved_at")),
                }
            )
        else:
            title = _clean_text(item)
            if title:
                normalized.append({"title": title[:200]})
    return normalized


def _decode_content_invitation(row: Row) -> Dict[str, Any]:
    item = dict(row)
    title_items_json = item.pop("title_items_json", None)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["title_items"] = json.loads(title_items_json or "[]")
    except json.JSONDecodeError:
        item["title_items"] = []
        item["title_items_decode_error"] = True
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_content_invitation_preference(row: Row) -> Dict[str, Any]:
    item = dict(row)
    metadata_json = item.pop("metadata_json", None)
    try:
        item["metadata"] = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def create_content_invitation(
    *,
    account_id: str,
    topic: str,
    invitation_text: str,
    title_items: List[Dict[str, Any]],
    invitation_id: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    source_task_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_topic = _clean_text(topic)
    cleaned_invitation_text = _clean_text(invitation_text)
    cleaned_invitation_id = _clean_text(invitation_id) or _new_id("cinv")
    normalized_titles = _normalize_title_items(title_items)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_topic:
        raise ValueError("topic is required")
    if not cleaned_invitation_text:
        raise ValueError("invitation_text is required")
    if len(normalized_titles) < 1:
        raise ValueError("title_items is required")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO content_invitations(
                id, account_id, topic, invitation_text, title_items_json,
                scheduled_at, expires_at, source_task_id, metadata_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                cleaned_invitation_id,
                cleaned_account_id,
                cleaned_topic,
                cleaned_invitation_text,
                json.dumps(normalized_titles, ensure_ascii=False),
                _clean_text(scheduled_at),
                _clean_text(expires_at),
                _clean_text(source_task_id),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (cleaned_invitation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_invitation was not created")
    return _decode_content_invitation(row)


def get_content_invitation(*, invitation_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (_clean_text(invitation_id),),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def list_content_invitations_for_account(
    *,
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = ["account_id = ?"]
    params: List[Any] = [account_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM content_invitations
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_content_invitation(row) for row in rows]


def get_active_content_invitation(
    *,
    account_id: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM content_invitations
            WHERE account_id = ?
              AND status = 'invited'
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY invited_at DESC, updated_at DESC
            LIMIT 1
            """,
            (account_id, now),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def claim_content_invitation_for_send(
    *,
    invitation_id: str,
) -> Optional[Dict[str, Any]]:
    """Atomically claim a candidate invitation for sending.

    Send timing is governed by the unified reactivation candidate (slots), so
    this claim does NOT gate on scheduled_at; it only requires the row to still
    be a 'candidate' for an active account and transitions it to 'sending'.
    Returns the claimed row, or None if it was already claimed/sent or the
    account is not active.
    """
    cleaned_invitation_id = _clean_text(invitation_id)
    if not cleaned_invitation_id:
        raise ValueError("invitation_id is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'sending',
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'candidate'
              AND EXISTS (
                  SELECT 1 FROM accounts
                  WHERE accounts.id = content_invitations.account_id
                    AND accounts.status = 'active'
                    AND accounts.app_id = ?
              )
            """,
            (cleaned_invitation_id, ZHAOXI_APP_ID),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (cleaned_invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def release_content_invitation_claim(
    *,
    invitation_id: str,
) -> bool:
    """Reset a 'sending' invitation back to 'candidate' so it can be retried."""
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'candidate',
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'sending'
            """,
            (invitation_id,),
        )
    return cursor.rowcount == 1


def mark_content_invitation_invited(
    *,
    invitation_id: str,
    outbound_message_id: Optional[int],
    invited_at: str,
    expires_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'invited',
                invited_at = ?,
                expires_at = COALESCE(?, expires_at),
                outbound_message_id = ?,
                policy_reason = NULL,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'sending'
            """,
            (invited_at, _clean_text(expires_at), outbound_message_id, invitation_id),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_rejected_by_policy(
    *,
    invitation_id: str,
    outbound_message_id: Optional[int],
    policy_reason: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = 'rejected_by_policy',
                outbound_message_id = COALESCE(?, outbound_message_id),
                policy_reason = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (outbound_message_id, _clean_text(policy_reason), invitation_id),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_titles_sent(
    *,
    invitation_id: str,
    trigger_message_id: Optional[str],
    tool_invocation_id: Optional[int],
    responded_at: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = 'titles_sent',
                responded_at = ?,
                trigger_message_id = ?,
                tool_invocation_id = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status = 'invited'
            """,
            (responded_at, _clean_text(trigger_message_id), tool_invocation_id, invitation_id),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def mark_content_invitation_feedback(
    *,
    invitation_id: str,
    status: str,
    trigger_message_id: Optional[str],
    tool_invocation_id: Optional[int],
    responded_at: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_content_invitation(invitation_id=invitation_id)
    if current is None:
        return None
    next_metadata = dict(current.get("metadata") or {})
    if metadata:
        next_metadata.update(metadata)
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_invitations
            SET status = ?,
                responded_at = ?,
                trigger_message_id = ?,
                tool_invocation_id = ?,
                metadata_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                _clean_text(status) or "declined",
                responded_at,
                _clean_text(trigger_message_id),
                tool_invocation_id,
                json.dumps(next_metadata, ensure_ascii=False),
                invitation_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_invitations WHERE id = ?",
            (invitation_id,),
        ).fetchone()
    return _decode_content_invitation(row) if row else None


def expire_content_invitations(*, now: str, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM content_invitations
            WHERE status = 'invited'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE content_invitations
                SET status = 'expired',
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            rows = conn.execute(
                f"SELECT * FROM content_invitations WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        else:
            rows = []
    return [_decode_content_invitation(row) for row in rows]


def get_content_invitation_preference(
    *,
    account_id: str,
    topic: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM content_invitation_preferences
            WHERE account_id = ? AND topic = ?
            """,
            (account_id, topic),
        ).fetchone()
    return _decode_content_invitation_preference(row) if row else None


def upsert_content_invitation_preference(
    *,
    account_id: str,
    topic: str,
    status: str,
    cooldown_until: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_topic = _clean_text(topic)
    cleaned_status = _clean_text(status) or "allowed"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_topic:
        raise ValueError("topic is required")
    current = get_content_invitation_preference(
        account_id=cleaned_account_id,
        topic=cleaned_topic,
    )
    next_metadata = dict((current or {}).get("metadata") or {})
    if metadata:
        next_metadata.update(metadata)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO content_invitation_preferences(
                account_id, topic, status, cooldown_until, last_feedback_at,
                feedback_count, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), 1, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id, topic) DO UPDATE SET
                status = excluded.status,
                cooldown_until = excluded.cooldown_until,
                last_feedback_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                feedback_count = content_invitation_preferences.feedback_count + 1,
                metadata_json = excluded.metadata_json,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                cleaned_account_id,
                cleaned_topic,
                cleaned_status,
                _clean_text(cooldown_until),
                json.dumps(next_metadata, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM content_invitation_preferences
            WHERE account_id = ? AND topic = ?
            """,
            (cleaned_account_id, cleaned_topic),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_invitation_preference was not created")
    return _decode_content_invitation_preference(row)
