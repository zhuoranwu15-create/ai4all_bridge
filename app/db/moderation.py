"""app.db.moderation — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import Connection, Row
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.db._core import (
    _clean_text,
    _decode_json_field,
    _new_id,
    connect,
)
__all__ = [
    'claim_content_moderation_task',
    'claim_queued_content_moderation_tasks',
    'create_content_moderation_export',
    'create_content_moderation_task',
    'get_content_moderation_export',
    'get_content_moderation_stats',
    'get_content_moderation_task',
    'get_content_moderation_task_by_idempotency_key',
    'get_moderation_account_risk_state',
    'insert_content_moderation_action',
    'insert_content_moderation_result',
    'list_content_moderation_actions',
    'list_content_moderation_results',
    'list_content_moderation_tasks',
    'update_content_moderation_task_machine_status',
    'update_content_moderation_task_review_status',
    'update_moderation_account_risk_controls',
    'upsert_moderation_account_risk_state',
]
# ---------------------------------------------------------------------------
# Content moderation
# ---------------------------------------------------------------------------

def _decode_content_moderation_task(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("risk_categories_json", "risk_categories", []),
        ("media_json", "media", {}),
        ("metadata_json", "metadata", {}),
    ):
        item = _decode_json_field(
            item,
            source_field=source_field,
            target_field=target_field,
            default=default,
        )
    return item


def _decode_content_moderation_result(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("categories_json", "categories", []),
        ("matched_terms_json", "matched_terms", []),
        ("raw_result_json", "raw_result", {}),
    ):
        item = _decode_json_field(
            item,
            source_field=source_field,
            target_field=target_field,
            default=default,
        )
    return item


def _decode_content_moderation_action(row: Row) -> Dict[str, Any]:
    return _decode_json_field(
        dict(row),
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )


def _decode_content_moderation_export(row: Row) -> Dict[str, Any]:
    return _decode_json_field(
        dict(row),
        source_field="artifact_json",
        target_field="artifact",
        default={},
    )


def create_content_moderation_task(
    *,
    account_id: str,
    session_id: Optional[int],
    source_type: str,
    source_id: str,
    message_db_id: Optional[int],
    outbound_message_id: Optional[int],
    direction: str,
    content_kind: str,
    status: str,
    risk_level: str,
    risk_categories: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    content_hash: Optional[str] = None,
    snapshot_text: Optional[str] = None,
    media: Optional[Dict[str, Any]] = None,
    sampling_reason: Optional[str] = None,
    sample_rate_percent: Optional[int] = None,
    policy_version: str = "moderation_policy_v1",
    prompt_version: Optional[str] = None,
    idempotency_key: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert a content moderation task, returning the existing row on retries."""

    cleaned_account_id = _clean_text(account_id)
    cleaned_source_type = _clean_text(source_type)
    cleaned_source_id = _clean_text(source_id)
    cleaned_direction = _clean_text(direction)
    cleaned_content_kind = _clean_text(content_kind)
    cleaned_status = _clean_text(status) or "queued"
    cleaned_risk_level = _clean_text(risk_level) or "unknown"
    cleaned_policy_version = _clean_text(policy_version)
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_source_type:
        raise ValueError("source_type is required")
    if not cleaned_source_id:
        raise ValueError("source_id is required")
    if not cleaned_direction:
        raise ValueError("direction is required")
    if not cleaned_content_kind:
        raise ValueError("content_kind is required")
    if not cleaned_policy_version:
        raise ValueError("policy_version is required")
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    task_id = _new_id("modtask")
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO content_moderation_tasks(
                id, account_id, session_id, source_type, source_id, message_db_id,
                outbound_message_id, direction, content_kind, status, risk_level,
                risk_categories_json, confidence, content_hash, snapshot_text,
                media_json, sampling_reason, sample_rate_percent, policy_version,
                prompt_version, idempotency_key, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                task_id,
                cleaned_account_id,
                session_id,
                cleaned_source_type,
                cleaned_source_id,
                message_db_id,
                outbound_message_id,
                cleaned_direction,
                cleaned_content_kind,
                cleaned_status,
                cleaned_risk_level,
                json.dumps(risk_categories or [], ensure_ascii=False),
                confidence,
                _clean_text(content_hash),
                snapshot_text,
                json.dumps(media or {}, ensure_ascii=False),
                _clean_text(sampling_reason),
                sample_rate_percent,
                cleaned_policy_version,
                _clean_text(prompt_version),
                cleaned_idempotency_key,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE idempotency_key = ?",
            (cleaned_idempotency_key,),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_moderation_task was not created")
    return _decode_content_moderation_task(row)


def get_content_moderation_task(*, task_id: str) -> Optional[Dict[str, Any]]:
    """Return one content moderation task by id."""

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return _decode_content_moderation_task(row) if row else None


def get_content_moderation_task_by_idempotency_key(
    *,
    idempotency_key: str,
) -> Optional[Dict[str, Any]]:
    """Return one content moderation task by its idempotency key."""

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    return _decode_content_moderation_task(row) if row else None


def list_content_moderation_tasks(
    *,
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    risk_level: Optional[str] = None,
    direction: Optional[str] = None,
    content_kind: Optional[str] = None,
    source_type: Optional[str] = None,
    assigned_admin_user_id: Optional[str] = None,
    public_or_assigned_admin_user_id: Optional[str] = None,
    review_queue_or_assigned_admin_user_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List moderation tasks with account/status/source filters."""

    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if risk_level:
        clauses.append("risk_level = ?")
        params.append(risk_level)
    if direction:
        clauses.append("direction = ?")
        params.append(direction)
    if content_kind:
        clauses.append("content_kind = ?")
        params.append(content_kind)
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)
    if assigned_admin_user_id:
        clauses.append("assigned_admin_user_id = ?")
        params.append(assigned_admin_user_id)
    if public_or_assigned_admin_user_id:
        clauses.append("(assigned_admin_user_id IS NULL OR assigned_admin_user_id = ?)")
        params.append(public_or_assigned_admin_user_id)
    if review_queue_or_assigned_admin_user_id:
        clauses.append(
            """
            (
                assigned_admin_user_id = ?
                OR (
                    assigned_admin_user_id IS NULL
                    AND status IN ('needs_review', 'reviewing', 'blocked', 'escalated')
                )
            )
            """
        )
        params.append(review_queue_or_assigned_admin_user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(500, int(limit))))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM content_moderation_tasks
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_content_moderation_task(row) for row in rows]


def claim_queued_content_moderation_tasks(
    *,
    batch_size: int,
    claim_timeout_seconds: int,
) -> List[Dict[str, Any]]:
    """Claim queued moderation tasks for a worker batch."""

    batch = max(1, min(500, int(batch_size)))
    stale_modifier = f"-{max(1, int(claim_timeout_seconds))} seconds"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM content_moderation_tasks
            WHERE status = 'queued'
              AND (
                machine_claimed_at IS NULL
                OR machine_claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?))
              )
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (stale_modifier, batch),
        ).fetchall()
        claimed: List[Dict[str, Any]] = []
        for row in rows:
            cursor = conn.execute(
                """
                UPDATE content_moderation_tasks
                SET machine_claimed_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    machine_attempts = machine_attempts + 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                  AND status = 'queued'
                  AND (
                    machine_claimed_at IS NULL
                    OR machine_claimed_at < strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours', ?))
                  )
                """,
                (row["id"], stale_modifier),
            )
            if cursor.rowcount != 1:
                continue
            claimed_row = conn.execute(
                "SELECT * FROM content_moderation_tasks WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if claimed_row is not None:
                claimed.append(_decode_content_moderation_task(claimed_row))
    return claimed


def update_content_moderation_task_machine_status(
    *,
    task_id: str,
    status: str,
    risk_level: str,
    risk_categories: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    last_error: Optional[str] = None,
    completed: bool = True,
    clear_claim: bool = True,
) -> Optional[Dict[str, Any]]:
    """Update task status after a machine review attempt."""

    with connect() as conn:
        conn.execute(
            """
            UPDATE content_moderation_tasks
            SET status = ?,
                risk_level = ?,
                risk_categories_json = ?,
                confidence = ?,
                last_error = ?,
                machine_completed_at = CASE
                    WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                    ELSE machine_completed_at
                END,
                machine_claimed_at = CASE WHEN ? THEN NULL ELSE machine_claimed_at END,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status,
                risk_level,
                json.dumps(risk_categories or [], ensure_ascii=False),
                confidence,
                last_error,
                1 if completed else 0,
                1 if clear_claim else 0,
                task_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return _decode_content_moderation_task(row) if row else None


def insert_content_moderation_result(
    *,
    task_id: str,
    account_id: str,
    reviewer_type: str,
    engine: Optional[str],
    engine_version: Optional[str],
    result_level: str,
    categories: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    matched_terms: Optional[List[Dict[str, Any]]] = None,
    reason: Optional[str] = None,
    raw_result: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert one machine or human moderation result row."""

    cleaned_task_id = _clean_text(task_id)
    cleaned_account_id = _clean_text(account_id)
    cleaned_reviewer_type = _clean_text(reviewer_type)
    cleaned_result_level = _clean_text(result_level)
    if not cleaned_task_id:
        raise ValueError("task_id is required")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_reviewer_type:
        raise ValueError("reviewer_type is required")
    if not cleaned_result_level:
        raise ValueError("result_level is required")

    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO content_moderation_results(
                task_id, account_id, reviewer_type, engine, engine_version,
                result_level, categories_json, confidence, matched_terms_json,
                reason, raw_result_json, latency_ms, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_task_id,
                cleaned_account_id,
                cleaned_reviewer_type,
                _clean_text(engine),
                _clean_text(engine_version),
                cleaned_result_level,
                json.dumps(categories or [], ensure_ascii=False),
                confidence,
                json.dumps(matched_terms or [], ensure_ascii=False),
                _clean_text(reason),
                json.dumps(raw_result or {}, ensure_ascii=False),
                latency_ms,
                error,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_results WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_moderation_result was not created")
    return _decode_content_moderation_result(row)


def list_content_moderation_results(*, task_id: str) -> List[Dict[str, Any]]:
    """List moderation result rows for a task in insertion order."""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM content_moderation_results
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
    return [_decode_content_moderation_result(row) for row in rows]


def claim_content_moderation_task(
    *,
    task_id: str,
    admin_user_id: str,
) -> Optional[Dict[str, Any]]:
    """Assign a human moderation task and move it into reviewing."""

    current = get_content_moderation_task(task_id=task_id)
    if current is None:
        return None
    if current.get("status") == "reviewing" and current.get("assigned_admin_user_id") not in (None, admin_user_id):
        return current
    if current.get("status") not in {"needs_review", "reviewing", "blocked", "escalated"}:
        return current
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE content_moderation_tasks
            SET status = 'reviewing',
                assigned_admin_user_id = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status IN ('needs_review', 'reviewing', 'blocked', 'escalated')
              AND (assigned_admin_user_id IS NULL OR assigned_admin_user_id = ?)
            """,
            (admin_user_id, task_id, admin_user_id),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT * FROM content_moderation_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return _decode_content_moderation_task(row) if row else None
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return _decode_content_moderation_task(row) if row else None


def update_content_moderation_task_review_status(
    *,
    task_id: str,
    status: str,
    risk_level: Optional[str] = None,
    risk_categories: Optional[List[str]] = None,
    reviewed_by_admin_user_id: Optional[str] = None,
    reviewed_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a moderation task after human review or admin action."""

    current = get_content_moderation_task(task_id=task_id)
    if current is None:
        return None
    next_risk_level = risk_level if risk_level is not None else current.get("risk_level")
    next_categories = risk_categories if risk_categories is not None else current.get("risk_categories") or []
    with connect() as conn:
        conn.execute(
            """
            UPDATE content_moderation_tasks
            SET status = ?,
                risk_level = ?,
                risk_categories_json = ?,
                reviewed_by_admin_user_id = COALESCE(?, reviewed_by_admin_user_id),
                reviewed_at = COALESCE(?, reviewed_at),
                last_error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status,
                next_risk_level,
                json.dumps(next_categories, ensure_ascii=False),
                reviewed_by_admin_user_id,
                reviewed_at,
                last_error,
                task_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return _decode_content_moderation_task(row) if row else None


def insert_content_moderation_action(
    *,
    task_id: str,
    account_id: str,
    admin_user_id: Optional[str],
    action: str,
    previous_status: Optional[str] = None,
    next_status: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record one human moderation action."""

    cleaned_task_id = _clean_text(task_id)
    cleaned_account_id = _clean_text(account_id)
    cleaned_action = _clean_text(action)
    if not cleaned_task_id:
        raise ValueError("task_id is required")
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_action:
        raise ValueError("action is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO content_moderation_actions(
                task_id, account_id, admin_user_id, action,
                previous_status, next_status, reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_task_id,
                cleaned_account_id,
                _clean_text(admin_user_id),
                cleaned_action,
                _clean_text(previous_status),
                _clean_text(next_status),
                _clean_text(reason),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_actions WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_moderation_action was not created")
    return _decode_content_moderation_action(row)


def list_content_moderation_actions(*, task_id: str) -> List[Dict[str, Any]]:
    """List human moderation actions for one task."""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM content_moderation_actions
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
    return [_decode_content_moderation_action(row) for row in rows]


def create_content_moderation_export(
    *,
    export_id: Optional[str] = None,
    task_id: str,
    account_id: str,
    admin_user_id: str,
    reason: str,
    artifact_path: Optional[str],
    artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create one moderation export record."""

    cleaned_export_id = _clean_text(export_id) or _new_id("modexport")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO content_moderation_exports(
                id, task_id, account_id, admin_user_id,
                reason, artifact_path, artifact_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_export_id,
                task_id,
                account_id,
                admin_user_id,
                reason,
                artifact_path,
                json.dumps(artifact or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM content_moderation_exports WHERE id = ?",
            (cleaned_export_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("content_moderation_export was not created")
    return _decode_content_moderation_export(row)


def get_content_moderation_export(*, export_id: str) -> Optional[Dict[str, Any]]:
    """Return one moderation export record by id."""

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_moderation_exports WHERE id = ?",
            (export_id,),
        ).fetchone()
    return _decode_content_moderation_export(row) if row else None


def get_content_moderation_stats(*, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Return aggregate moderation counts for admin stats."""

    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM content_moderation_tasks {where}",
            params,
        ).fetchone()["c"]
        by_status_rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS c
            FROM content_moderation_tasks
            {where}
            GROUP BY status
            """,
            params,
        ).fetchall()
        by_risk_rows = conn.execute(
            f"""
            SELECT risk_level, COUNT(*) AS c
            FROM content_moderation_tasks
            {where}
            GROUP BY risk_level
            """,
            params,
        ).fetchall()
        by_direction_rows = conn.execute(
            f"""
            SELECT direction, COUNT(*) AS c
            FROM content_moderation_tasks
            {where}
            GROUP BY direction
            """,
            params,
        ).fetchall()
        review_clauses = clauses + ["status IN ('needs_review', 'reviewing', 'escalated')"]
        review_where = f"WHERE {' AND '.join(review_clauses)}"
        review_queue_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM content_moderation_tasks {review_where}",
            params,
        ).fetchone()["c"]
    return {
        "total": int(total),
        "review_queue_count": int(review_queue_count),
        "by_status": {str(row["status"]): int(row["c"]) for row in by_status_rows},
        "by_risk_level": {str(row["risk_level"]): int(row["c"]) for row in by_risk_rows},
        "by_direction": {str(row["direction"]): int(row["c"]) for row in by_direction_rows},
    }


def _decode_moderation_account_risk_state(row: Row) -> Dict[str, Any]:
    return _decode_json_field(
        dict(row),
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )


def get_moderation_account_risk_state(*, account_id: str) -> Optional[Dict[str, Any]]:
    """Return moderation risk state for one account."""

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM moderation_account_risk_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return _decode_moderation_account_risk_state(row) if row else None


def upsert_moderation_account_risk_state(
    *,
    account_id: str,
    risk_level: str,
    risk_score_delta: int = 0,
    sample_multiplier: float = 1.0,
    last_risk_at: Optional[str] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update account-level moderation risk state used by sampling policy."""

    current = get_moderation_account_risk_state(account_id=account_id)
    current_metadata = current.get("metadata", {}) if current else {}
    next_metadata = {
        **(current_metadata if isinstance(current_metadata, dict) else {}),
        **(metadata_patch or {}),
    }
    current_score = int(current.get("risk_score", 0)) if current else 0
    next_score = max(0, current_score + int(risk_score_delta or 0))
    current_multiplier = float(current.get("sample_multiplier", 1.0)) if current else 1.0
    next_multiplier = max(current_multiplier, float(sample_multiplier or 1.0))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO moderation_account_risk_state(
                account_id, risk_level, risk_score, sample_multiplier,
                last_risk_at, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))), ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id) DO UPDATE SET
                risk_level = excluded.risk_level,
                risk_score = excluded.risk_score,
                sample_multiplier = excluded.sample_multiplier,
                last_risk_at = excluded.last_risk_at,
                metadata_json = excluded.metadata_json,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                account_id,
                risk_level,
                next_score,
                next_multiplier,
                last_risk_at,
                json.dumps(next_metadata, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM moderation_account_risk_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("moderation_account_risk_state was not created")
    return _decode_moderation_account_risk_state(row)


def update_moderation_account_risk_controls(
    *,
    account_id: str,
    risk_level: Optional[str] = None,
    proactive_blocked_until: Optional[str] = None,
    conversation_blocked_until: Optional[str] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update account-level moderation controls such as proactive mute windows."""

    current = get_moderation_account_risk_state(account_id=account_id)
    current_metadata = current.get("metadata", {}) if current else {}
    next_metadata = {
        **(current_metadata if isinstance(current_metadata, dict) else {}),
        **(metadata_patch or {}),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO moderation_account_risk_state(
                account_id, risk_level, risk_score, sample_multiplier,
                proactive_blocked_until, conversation_blocked_until,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id) DO UPDATE SET
                risk_level = excluded.risk_level,
                proactive_blocked_until = excluded.proactive_blocked_until,
                conversation_blocked_until = excluded.conversation_blocked_until,
                metadata_json = excluded.metadata_json,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                account_id,
                risk_level or (current.get("risk_level") if current else "normal"),
                int(current.get("risk_score", 0)) if current else 0,
                float(current.get("sample_multiplier", 1.0)) if current else 1.0,
                proactive_blocked_until if proactive_blocked_until is not None else (current.get("proactive_blocked_until") if current else None),
                conversation_blocked_until if conversation_blocked_until is not None else (current.get("conversation_blocked_until") if current else None),
                json.dumps(next_metadata, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM moderation_account_risk_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("moderation_account_risk_state was not created")
    return _decode_moderation_account_risk_state(row)


def _delete_content_moderation_tasks_where(
    conn: Connection,
    where_sql: str,
    params: tuple,
) -> Dict[str, int]:
    task_select_sql = f"SELECT id FROM content_moderation_tasks WHERE {where_sql}"
    actions = conn.execute(
        f"DELETE FROM content_moderation_actions WHERE task_id IN ({task_select_sql})",
        params,
    ).rowcount
    exports = conn.execute(
        f"DELETE FROM content_moderation_exports WHERE task_id IN ({task_select_sql})",
        params,
    ).rowcount
    results = conn.execute(
        f"DELETE FROM content_moderation_results WHERE task_id IN ({task_select_sql})",
        params,
    ).rowcount
    tasks = conn.execute(
        f"DELETE FROM content_moderation_tasks WHERE {where_sql}",
        params,
    ).rowcount
    return {
        "moderation_actions_deleted": actions,
        "moderation_exports_deleted": exports,
        "moderation_results_deleted": results,
        "moderation_tasks_deleted": tasks,
    }


