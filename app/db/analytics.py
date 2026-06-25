"""app.db.analytics — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import Connection, IntegrityError, Row
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.db._core import (
    _clean_text,
    _savepoint,
    _tx,
    connect,
)
__all__ = [
    'create_search_provider_run',
    'create_tool_invocation',
    'get_debug_trace',
    'get_tool_invocation',
    'insert_debug_trace',
    'list_debug_traces',
    'list_recent_tool_invocations_for_replay',
    'list_search_provider_runs',
    'list_tool_invocations',
    'update_tool_invocation',
]
# ---------------------------------------------------------------------------
# Debug traces
# ---------------------------------------------------------------------------

def insert_debug_trace(
    *,
    trace_id: str,
    account_id: str,
    session_id: int,
    message_id: Optional[str],
    source: str,
    llm_model: Optional[str],
    system_prompt: Optional[str],
    messages: List[Dict[str, Any]],
    reply: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[int]:
    try:
        with _tx(conn) as tx:
            # _savepoint 保证 IntegrityError 只回滚到保存点，不污染外部事务（PG 下必须）。
            with _savepoint(tx, "insert_trace"):
                cursor = tx.execute(
                    """
                    INSERT INTO debug_traces(
                        trace_id, account_id, session_id, message_id, source,
                        llm_model, system_prompt, messages_json, reply,
                        metadata_json, latency_ms, error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace_id,
                        account_id,
                        session_id,
                        message_id,
                        source,
                        llm_model,
                        system_prompt,
                        json.dumps(messages, ensure_ascii=False),
                        reply,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        latency_ms,
                        error,
                    ),
                )
                return int(cursor.lastrowid)
    except IntegrityError:
        return None


def list_debug_traces(
    *,
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                id, trace_id, account_id, session_id, message_id, source,
                llm_model, reply, latency_ms, error, created_at
            FROM debug_traces
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_debug_trace(*, trace_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, trace_id, account_id, session_id, message_id, source,
                llm_model, system_prompt, messages_json, reply,
                metadata_json, latency_ms, error, created_at
            FROM debug_traces
            WHERE trace_id = ?
            """,
            (trace_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["messages"] = json.loads(item.pop("messages_json") or "[]")
    except json.JSONDecodeError:
        item["messages"] = []
        item["messages_decode_error"] = True
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


# ---------------------------------------------------------------------------
# Tool invocations / search provider runs
# ---------------------------------------------------------------------------

def _decode_tool_invocation(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("args_json", "args", {}),
        ("result_json", "result", {}),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or json.dumps(default))
        except json.JSONDecodeError:
            value = default
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value
    return item


def _decode_search_provider_run(row: Row) -> Dict[str, Any]:
    item = dict(row)
    for source_field, target_field, default in (
        ("request_json", "request", {}),
        ("response_json", "response", {}),
    ):
        raw_json = item.pop(source_field, None)
        try:
            value = json.loads(raw_json or json.dumps(default))
        except json.JSONDecodeError:
            value = default
            item[f"{target_field}_decode_error"] = True
        item[target_field] = value
    return item


def create_tool_invocation(
    *,
    account_id: str,
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    session_id: Optional[int] = None,
    message_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    status: str = "running",
    result: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_tool_name = _clean_text(tool_name)
    cleaned_status = _clean_text(status) or "running"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_tool_name:
        raise ValueError("tool_name is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tool_invocations(
                account_id, session_id, message_id, tool_call_id, tool_name,
                status, args_json, result_json, latency_ms, error,
                created_at, finished_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                    CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE NULL END,
                    strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                cleaned_account_id,
                session_id,
                _clean_text(message_id),
                _clean_text(tool_call_id),
                cleaned_tool_name,
                cleaned_status,
                json.dumps(args or {}, ensure_ascii=False),
                json.dumps(result or {}, ensure_ascii=False),
                latency_ms,
                _clean_text(error),
                1 if finished else 0,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("tool_invocation was not created")
    return _decode_tool_invocation(row)


def get_tool_invocation(*, tool_invocation_id: int) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (tool_invocation_id,),
        ).fetchone()
    return _decode_tool_invocation(row) if row else None


def list_tool_invocations(
    *,
    account_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if tool_name:
        clauses.append("tool_name = ?")
        params.append(tool_name)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM tool_invocations
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_tool_invocation(row) for row in rows]


def update_tool_invocation(
    *,
    tool_invocation_id: int,
    status: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Optional[Dict[str, Any]]:
    current = get_tool_invocation(tool_invocation_id=tool_invocation_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE tool_invocations
            SET status = COALESCE(?, status),
                result_json = COALESCE(?, result_json),
                latency_ms = COALESCE(?, latency_ms),
                error = ?,
                finished_at = CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE finished_at END,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                _clean_text(status),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                latency_ms,
                _clean_text(error),
                1 if finished else 0,
                tool_invocation_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM tool_invocations WHERE id = ?",
            (tool_invocation_id,),
        ).fetchone()
    return _decode_tool_invocation(row) if row else None


def list_recent_tool_invocations_for_replay(
    account_id: str,
    *,
    message_ids: List[str],
) -> List[Dict[str, Any]]:
    """返回给定 inbound message_id 列表对应的 tool_invocations（已完成），按 id ASC 排序。

    用于 Batch C tool 证据跨 turn 回灌：调用方传最近 K 轮的 user message_id，
    此函数返回这几轮里所有已完成的工具调用，供 inject_tool_evidence_replay 重建 wire。
    """
    if not message_ids:
        return []
    placeholders = ",".join("?" * len(message_ids))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, message_id, tool_call_id, tool_name, status, args_json, result_json
            FROM tool_invocations
            WHERE account_id = ?
              AND message_id IN ({placeholders})
              AND status IN ('succeeded', 'failed')
              AND tool_call_id IS NOT NULL AND tool_call_id != ''
            ORDER BY id ASC
            """,
            [account_id] + list(message_ids),
        ).fetchall()
    return [_decode_tool_invocation(row) for row in rows]


def create_search_provider_run(
    *,
    account_id: str,
    provider: str,
    request: Optional[Dict[str, Any]] = None,
    tool_invocation_id: Optional[int] = None,
    task_id: Optional[str] = None,
    attempt: int = 1,
    status: str = "running",
    response: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> Dict[str, Any]:
    cleaned_account_id = _clean_text(account_id)
    cleaned_provider = _clean_text(provider)
    cleaned_status = _clean_text(status) or "running"
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    if not cleaned_provider:
        raise ValueError("provider is required")
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO search_provider_runs(
                tool_invocation_id, task_id, account_id, provider, attempt,
                status, request_json, response_json, finished_at, latency_ms,
                error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE NULL END,
                    ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                tool_invocation_id,
                _clean_text(task_id),
                cleaned_account_id,
                cleaned_provider,
                max(1, int(attempt or 1)),
                cleaned_status,
                json.dumps(request or {}, ensure_ascii=False),
                json.dumps(response or {}, ensure_ascii=False),
                1 if finished else 0,
                latency_ms,
                _clean_text(error),
            ),
        )
        row = conn.execute(
            "SELECT * FROM search_provider_runs WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    if row is None:
        raise RuntimeError("search_provider_run was not created")
    return _decode_search_provider_run(row)


def list_search_provider_runs(
    *,
    account_id: Optional[str] = None,
    tool_invocation_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if tool_invocation_id is not None:
        clauses.append("tool_invocation_id = ?")
        params.append(tool_invocation_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM search_provider_runs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode_search_provider_run(row) for row in rows]


