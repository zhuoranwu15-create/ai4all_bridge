"""Persistence for provider-neutral Human-AI streaming turn runs."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.db._backend import IntegrityError
from app.db._core import connect

__all__ = [
    "ActiveRuntimeTurnError",
    "create_runtime_turn_run",
    "finish_runtime_turn_run",
    "get_runtime_turn_run",
    "get_runtime_turn_run_by_idempotency",
    "mark_runtime_turn_first_delta",
    "mark_runtime_turn_running",
    "request_runtime_turn_cancel",
    "reclaim_stale_runtime_turn_runs",
    "runtime_turn_cancel_requested",
]


class ActiveRuntimeTurnError(RuntimeError):
    """Raised when a session already owns an accepted/running turn."""


def _row_dict(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def get_runtime_turn_run(turn_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM runtime_turn_runs WHERE id=?",
            (turn_id,),
        ).fetchone()
    return _row_dict(row)


def get_runtime_turn_run_by_idempotency(
    *, app_id: str, account_id: str, idempotency_key: str
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM runtime_turn_runs
            WHERE app_id=? AND account_id=? AND idempotency_key=?
            """,
            (app_id, account_id, idempotency_key),
        ).fetchone()
    return _row_dict(row)


def create_runtime_turn_run(
    *,
    turn_id: str,
    app_id: str,
    account_id: str,
    session_id: int,
    client_message_id: str,
    idempotency_key: str,
    provider_id: Optional[str],
    model_ref: Optional[str],
) -> Tuple[Dict[str, Any], bool]:
    """Create an accepted run, returning an idempotent existing row when present."""
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_turn_runs(
                    id, app_id, account_id, session_id, client_message_id,
                    idempotency_key, status, provider_id, model_ref
                ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    turn_id,
                    app_id,
                    account_id,
                    int(session_id),
                    client_message_id,
                    idempotency_key,
                    provider_id,
                    model_ref,
                ),
            )
    except IntegrityError as err:
        existing = get_runtime_turn_run_by_idempotency(
            app_id=app_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing, False
        raise ActiveRuntimeTurnError("runtime_turn_active") from err
    created = get_runtime_turn_run(turn_id)
    if created is None:
        raise RuntimeError("runtime_turn_create_lost")
    return created, True


def mark_runtime_turn_running(turn_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runtime_turn_runs
            SET status='running',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status='accepted'
            """,
            (turn_id,),
        )
        return cursor.rowcount == 1


def mark_runtime_turn_first_delta(turn_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runtime_turn_runs
            SET first_delta_at=COALESCE(
                    first_delta_at,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                ),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status IN ('accepted', 'running')
              AND cancel_requested_at IS NULL
            """,
            (turn_id,),
        )
        return cursor.rowcount == 1


def request_runtime_turn_cancel(
    *, turn_id: str, app_id: str, account_id: str, session_id: int
) -> bool:
    """Request cooperative cancellation without prematurely claiming a terminal state."""
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runtime_turn_runs
            SET cancel_requested_at=COALESCE(
                    cancel_requested_at,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                ),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND app_id=? AND account_id=? AND session_id=?
              AND status IN ('accepted', 'running')
            """,
            (turn_id, app_id, account_id, int(session_id)),
        )
        return cursor.rowcount == 1


def runtime_turn_cancel_requested(turn_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT cancel_requested_at FROM runtime_turn_runs
            WHERE id=? AND status IN ('accepted', 'running')
            """,
            (turn_id,),
        ).fetchone()
    return row is not None and row["cancel_requested_at"] is not None


def finish_runtime_turn_run(
    *,
    turn_id: str,
    status: str,
    assistant_message_id: Optional[str] = None,
    finish_reason: Optional[str] = None,
    error_code: Optional[str] = None,
) -> bool:
    if status not in {"completed", "cancelled", "failed", "abandoned"}:
        raise ValueError(f"invalid runtime turn terminal status: {status}")
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runtime_turn_runs
            SET status=?, assistant_message_id=?, finish_reason=?, error_code=?,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                completed_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status IN ('accepted', 'running')
            """,
            (status, assistant_message_id, finish_reason, error_code, turn_id),
        )
        return cursor.rowcount == 1


def reclaim_stale_runtime_turn_runs(
    *,
    ttl_seconds: int = 600,
    app_id: Optional[str] = None,
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Mark stale active runs abandoned, optionally scoped to one product session."""
    modifier = f"-{max(1, int(ttl_seconds))} seconds"
    filters = [
        "status IN ('accepted', 'running')",
        "updated_at < to_char((now() AT TIME ZONE 'Asia/Shanghai') + (?)::interval, 'YYYY-MM-DD HH24:MI:SS')",
    ]
    params: List[Any] = [modifier]
    if app_id is not None:
        filters.append("app_id=?")
        params.append(app_id)
    if account_id is not None:
        filters.append("account_id=?")
        params.append(account_id)
    if session_id is not None:
        filters.append("session_id=?")
        params.append(int(session_id))
    where_sql = " AND ".join(filters)
    reclaimed_ids: List[str] = []
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM runtime_turn_runs WHERE {where_sql}",
            tuple(params),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        for turn_id in ids:
            cursor = conn.execute(
                """
                UPDATE runtime_turn_runs
                SET status='abandoned', error_code='runtime_turn_timeout',
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    completed_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=? AND status IN ('accepted', 'running')
                  AND updated_at < to_char((now() AT TIME ZONE 'Asia/Shanghai') + (?)::interval, 'YYYY-MM-DD HH24:MI:SS')
                """,
                (turn_id, modifier),
            )
            if cursor.rowcount == 1:
                reclaimed_ids.append(turn_id)
    return [
        run
        for turn_id in reclaimed_ids
        if (run := get_runtime_turn_run(turn_id)) is not None
    ]
