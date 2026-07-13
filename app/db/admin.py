"""app.db.admin — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
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
from app.db._core import (
    DEFAULT_ACTIVE_SESSION_KEYS,
    _clean_text,
    _decode_json_field,
    connect,
)
__all__ = [
    'close_session',
    'create_admin_plaintext_grant',
    'create_dreaming_run',
    'find_active_admin_plaintext_grant',
    'get_admin_plaintext_grant',
    'get_admin_user',
    'get_dreaming_memory_item',
    'get_dreaming_run',
    'insert_admin_access_event',
    'insert_dreaming_memory_item',
    'insert_memory_event',
    'list_active_sessions_for_business_day_before',
    'list_admin_access_events',
    'list_admin_plaintext_grants',
    'list_admin_users',
    'list_dreaming_memory_items',
    'list_dreaming_runs',
    'list_memory_events',
    'update_admin_plaintext_grant_status',
    'update_dreaming_memory_item_status',
    'update_dreaming_run',
    'update_session_summary',
    'upsert_admin_user',
]
# ---------------------------------------------------------------------------
# Admin access events
# ---------------------------------------------------------------------------

def upsert_admin_user(
    *,
    admin_user_id: str,
    role: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    status: str = "active",
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_users(id, email, display_name, role, status, updated_at)
            VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET
                email = COALESCE(excluded.email, admin_users.email),
                display_name = COALESCE(excluded.display_name, admin_users.display_name),
                role = excluded.role,
                status = excluded.status,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (admin_user_id, email, display_name, role, status),
        )
        row = conn.execute(
            "SELECT * FROM admin_users WHERE id = ?",
            (admin_user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("admin_user was not created")
    return dict(row)


def get_admin_user(*, admin_user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE id = ?",
            (admin_user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_admin_users(*, limit: int = 100) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM admin_users
            ORDER BY role ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _decode_admin_plaintext_grant(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field in (
        ("account_scope_json", "account_scope"),
        ("resource_scope_json", "resource_scope"),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or "[]")
        except json.JSONDecodeError:
            value = []
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value if isinstance(value, list) else []
    return item


def create_admin_plaintext_grant(
    *,
    requester_admin_user_id: str,
    reason: str,
    account_scope: Optional[List[str]] = None,
    resource_scope: Optional[List[str]] = None,
    time_scope_start: Optional[str] = None,
    time_scope_end: Optional[str] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO admin_plaintext_grants(
                requester_admin_user_id, reason, account_scope_json,
                resource_scope_json, time_scope_start, time_scope_end
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                requester_admin_user_id,
                reason,
                json.dumps(account_scope or [], ensure_ascii=False),
                json.dumps(resource_scope or [], ensure_ascii=False),
                time_scope_start,
                time_scope_end,
            ),
        )
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("admin_plaintext_grant was not created")
    return _decode_admin_plaintext_grant(row)


def get_admin_plaintext_grant(*, grant_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
    return _decode_admin_plaintext_grant(row) if row else None


def list_admin_plaintext_grants(
    *,
    requester_admin_user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if requester_admin_user_id:
        clauses.append("requester_admin_user_id = ?")
        params.append(requester_admin_user_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM admin_plaintext_grants
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_admin_plaintext_grant(row) for row in rows]


def update_admin_plaintext_grant_status(
    *,
    grant_id: int,
    status: str,
    approver_admin_user_id: Optional[str] = None,
    approved_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    revoked_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE admin_plaintext_grants
            SET status = ?,
                approver_admin_user_id = COALESCE(?, approver_admin_user_id),
                approved_at = COALESCE(?, approved_at),
                expires_at = COALESCE(?, expires_at),
                revoked_at = COALESCE(?, revoked_at),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status,
                approver_admin_user_id,
                approved_at,
                expires_at,
                revoked_at,
                grant_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM admin_plaintext_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
    return _decode_admin_plaintext_grant(row) if row else None


def find_active_admin_plaintext_grant(
    *,
    requester_admin_user_id: str,
    account_id: str,
    resource_type: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM admin_plaintext_grants
            WHERE requester_admin_user_id = ?
              AND status = 'approved'
              AND expires_at IS NOT NULL
              AND expires_at > ?
            ORDER BY expires_at DESC, id DESC
            """,
            (requester_admin_user_id, now),
        ).fetchall()
    for row in rows:
        grant = _decode_admin_plaintext_grant(row)
        account_scope = [str(item) for item in grant.get("account_scope") or []]
        resource_scope = [str(item) for item in grant.get("resource_scope") or []]
        if account_scope and account_id not in account_scope:
            continue
        if resource_scope and resource_type not in resource_scope:
            continue
        return grant
    return None


def insert_admin_access_event(
    *,
    admin_user_id: Optional[str],
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    account_id: Optional[str] = None,
    plaintext: bool = False,
    grant_id: Optional[int] = None,
    reason: Optional[str] = None,
    request_path: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO admin_access_events(
                admin_user_id, action, resource_type, resource_id, account_id,
                plaintext, grant_id, reason, request_path, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admin_user_id,
                action,
                resource_type,
                resource_id,
                account_id,
                1 if plaintext else 0,
                grant_id,
                reason,
                request_path,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)


def list_admin_access_events(
    *,
    account_id: Optional[str] = None,
    plaintext: Optional[bool] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if plaintext is not None:
        clauses.append("plaintext = ?")
        params.append(1 if plaintext else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, admin_user_id, action, resource_type, resource_id,
                account_id, plaintext, grant_id, reason, request_path,
                metadata_json, created_at
            FROM admin_access_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        item["plaintext"] = bool(item.get("plaintext"))
        events.append(
            _decode_json_field(
                item,
                source_field="metadata_json",
                target_field="metadata",
                default={},
            )
        )
    return events


def create_dreaming_run(
    *,
    account_id: str,
    source_type: str,
    source_session_id: Optional[int] = None,
    source_business_day: Optional[str] = None,
    status: str = "running",
    prompt_version: str,
    llm_model: Optional[str] = None,
    input_hash: Optional[str] = None,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO accounts(id, updated_at)
            VALUES (?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (account_id,),
        )
        cursor = conn.execute(
            """
            INSERT INTO dreaming_runs(
                account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, actor_type, actor_id,
                started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                account_id,
                source_type,
                source_session_id,
                source_business_day,
                status,
                prompt_version,
                llm_model,
                input_hash,
                actor_type,
                actor_id,
            ),
        )
        run_id = int(cursor.lastrowid)
    run = get_dreaming_run(run_id=run_id)
    if run is None:
        raise RuntimeError("dreaming_run was not created")
    return run


def update_dreaming_run(
    *,
    run_id: int,
    status: Optional[str] = None,
    output: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    token_input: Optional[int] = None,
    token_output: Optional[int] = None,
    completed: bool = False,
) -> Optional[Dict[str, Any]]:
    current = get_dreaming_run(run_id=run_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE dreaming_runs
            SET status = COALESCE(?, status),
                output_json = COALESCE(?, output_json),
                error = ?,
                token_input = COALESCE(?, token_input),
                token_output = COALESCE(?, token_output),
                completed_at = CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE completed_at END,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status,
                json.dumps(output, ensure_ascii=False) if output is not None else None,
                error,
                token_input,
                token_output,
                1 if completed else 0,
                run_id,
            ),
        )
    return get_dreaming_run(run_id=run_id)


def get_dreaming_run(*, run_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, output_json,
                error, token_input, token_output, actor_type, actor_id,
                started_at, completed_at, created_at, updated_at
            FROM dreaming_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return _decode_json_field(
        dict(row),
        source_field="output_json",
        target_field="output",
        default={},
    )


def list_dreaming_runs(
    *,
    account_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 200)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, source_type, source_session_id, source_business_day,
                status, prompt_version, llm_model, input_hash, output_json,
                error, token_input, token_output, actor_type, actor_id,
                started_at, completed_at, created_at, updated_at
            FROM dreaming_runs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        _decode_json_field(
            dict(row),
            source_field="output_json",
            target_field="output",
            default={},
        )
        for row in rows
    ]


def insert_dreaming_memory_item(
    *,
    account_id: str,
    dreaming_run_id: int,
    source_type: str,
    source_session_id: Optional[int],
    source_daily_note_date: Optional[str],
    operation: str,
    target_file: str,
    category: str,
    memory_text: str,
    base_text_hash: Optional[str] = None,
    diff: Optional[Dict[str, Any]] = None,
    importance: str = "medium",
    confidence: float = 0.0,
    sensitivity: str = "normal",
    apply_status: str = "generated",
    skip_reason: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO dreaming_memory_items(
                account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                dreaming_run_id,
                source_type,
                source_session_id,
                source_daily_note_date,
                operation,
                target_file,
                category,
                memory_text,
                base_text_hash,
                json.dumps(diff or {}, ensure_ascii=False),
                importance,
                float(confidence),
                sensitivity,
                apply_status,
                skip_reason,
                reason,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        item_id = int(cursor.lastrowid)
    item = get_dreaming_memory_item(item_id=item_id)
    if item is None:
        raise RuntimeError("dreaming_memory_item was not created")
    return item


def update_dreaming_memory_item_status(
    *,
    item_id: int,
    apply_status: str,
    skip_reason: Optional[str] = None,
    diff: Optional[Dict[str, Any]] = None,
    base_text_hash: Optional[str] = None,
    applied: bool = False,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE dreaming_memory_items
            SET apply_status = ?,
                skip_reason = ?,
                diff_json = COALESCE(?, diff_json),
                base_text_hash = COALESCE(?, base_text_hash),
                applied_at = CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE applied_at END
            WHERE id = ?
            """,
            (
                apply_status,
                skip_reason,
                json.dumps(diff, ensure_ascii=False) if diff is not None else None,
                base_text_hash,
                1 if applied else 0,
                item_id,
            ),
        )
    return get_dreaming_memory_item(item_id=item_id)


def get_dreaming_memory_item(*, item_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json, created_at, applied_at
            FROM dreaming_memory_items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    item = _decode_json_field(
        dict(row),
        source_field="diff_json",
        target_field="diff",
        default={},
    )
    return _decode_json_field(
        item,
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )


def list_dreaming_memory_items(
    *,
    account_id: Optional[str] = None,
    dreaming_run_id: Optional[int] = None,
    apply_status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if dreaming_run_id is not None:
        clauses.append("dreaming_run_id = ?")
        params.append(dreaming_run_id)
    if apply_status:
        clauses.append("apply_status = ?")
        params.append(apply_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, dreaming_run_id, source_type, source_session_id,
                source_daily_note_date, operation, target_file, category, memory_text,
                base_text_hash, diff_json, importance, confidence, sensitivity,
                apply_status, skip_reason, reason, metadata_json, created_at, applied_at
            FROM dreaming_memory_items
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    items = []
    for row in rows:
        item = _decode_json_field(
            dict(row),
            source_field="diff_json",
            target_field="diff",
            default={},
        )
        items.append(
            _decode_json_field(
                item,
                source_field="metadata_json",
                target_field="metadata",
                default={},
            )
        )
    return items


def insert_memory_event(
    *,
    account_id: str,
    memory_item_id: Optional[int],
    event_type: str,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
    before_text: Optional[str] = None,
    after_text: Optional[str] = None,
    diff_text: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_events(
                account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                memory_item_id,
                event_type,
                actor_type,
                actor_id,
                before_text,
                after_text,
                diff_text,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        event_id = int(cursor.lastrowid)
        row = conn.execute(
            """
            SELECT
                id, account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json, created_at
            FROM memory_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
    event = _decode_json_field(
        dict(row),
        source_field="metadata_json",
        target_field="metadata",
        default={},
    )
    return event


def list_memory_events(
    *,
    account_id: Optional[str] = None,
    memory_item_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if memory_item_id is not None:
        clauses.append("memory_item_id = ?")
        params.append(memory_item_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, account_id, memory_item_id, event_type, actor_type, actor_id,
                before_text, after_text, diff_text, metadata_json, created_at
            FROM memory_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        _decode_json_field(
            dict(row),
            source_field="metadata_json",
            target_field="metadata",
            default={},
        )
        for row in rows
    ]


def update_session_summary(
    *,
    session_id: int,
    session_summary: Optional[str],
    carryover_summary: Optional[str],
    summary_model: Optional[str],
    summary_prompt_version: Optional[str],
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET session_summary = COALESCE(?, session_summary),
                carryover_summary = COALESCE(?, carryover_summary),
                summary_model = COALESCE(?, summary_model),
                summary_prompt_version = COALESCE(?, summary_prompt_version),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                session_summary,
                carryover_summary,
                summary_model,
                summary_prompt_version,
                session_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def close_session(
    *,
    session_id: int,
    close_reason: str,
    archived_session_key: Optional[str] = None,
    session_summary: Optional[str] = None,
    carryover_summary: Optional[str] = None,
    summary_model: Optional[str] = None,
    summary_prompt_version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET session_key = COALESCE(?, session_key),
                status = 'closed',
                ended_at = COALESCE(ended_at, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
                close_reason = COALESCE(close_reason, ?),
                session_summary = COALESCE(?, session_summary),
                carryover_summary = COALESCE(?, carryover_summary),
                summary_model = COALESCE(?, summary_model),
                summary_prompt_version = COALESCE(?, summary_prompt_version),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                archived_session_key,
                close_reason,
                session_summary,
                carryover_summary,
                summary_model,
                summary_prompt_version,
                session_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_active_sessions_for_business_day_before(
    *,
    business_day: str,
    limit: int = 100,
    node_id: Optional[str] = None,
    active_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """node_id 非空时只返回归属该节点的账号的会话（厚节点改造 P4 调度分片）。

    ``active_keys`` 指定要扫描的合法 active scope（§7.1 / Codex ②）。默认扫描
    ``DEFAULT_ACTIVE_SESSION_KEYS``（微信 ``__account_active__`` + Web
    ``__web_active__``），使两 scope 的到期 active session 都能被每日轮转/dreaming 关闭；
    否则 Web scope 永不轮转。默认含微信 key，故对纯微信数据行为等价现状。
    """
    keys = list(active_keys) if active_keys else list(DEFAULT_ACTIVE_SESSION_KEYS)
    placeholders = ", ".join("?" for _ in keys)
    node_filter = _clean_text(node_id) if node_id else None
    safe_limit = max(1, min(int(limit), 500))
    with connect() as conn:
        if node_filter:
            rows = conn.execute(
                f"""
                SELECT s.*
                FROM sessions s
                JOIN accounts a ON a.id = s.account_id
                WHERE s.session_key IN ({placeholders})
                  AND s.status = 'active'
                  AND s.business_day IS NOT NULL
                  AND s.business_day != ''
                  AND s.business_day < ?
                  AND a.assigned_node_id = ?
                ORDER BY s.updated_at ASC, s.id ASC
                LIMIT ?
                """,
                (*keys, business_day, node_filter, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT * FROM sessions
                WHERE session_key IN ({placeholders})
                  AND status = 'active'
                  AND business_day IS NOT NULL
                  AND business_day != ''
                  AND business_day < ?
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (*keys, business_day, safe_limit),
            ).fetchall()
    return [dict(row) for row in rows]


