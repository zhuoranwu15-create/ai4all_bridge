"""M4 Companion World resident lifecycle 的审计型存储原语。

本模块只铺 event/action 数据基座；不可逆 ``committed`` 必须由 M4-3 的
offline+farewell+read-only 组合事务完成，禁止通过通用状态更新绕过原子边界。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "create_resident_lifecycle_event",
    "get_resident_lifecycle_event",
    "list_resident_lifecycle_evidence_tasks",
    "list_resident_lifecycle_event_actions",
    "list_resident_lifecycle_events_for_review",
    "list_resident_lifecycle_scopes",
    "transition_resident_lifecycle_event",
]

_EVENT_TYPES = {"inactivity", "value_misalignment", "severe_abuse"}
_OPEN_STATUSES = {"cooling_down", "review_pending"}
_TERMINAL_STATUSES = {"cancelled", "rejected"}
_ACTOR_TYPES = {"scheduler", "admin", "system"}
_ALLOWED_TRANSITIONS = {
    "cooling_down": {"review_pending", "cancelled", "rejected"},
    "review_pending": {"cancelled", "rejected"},
}


@contextmanager
def _lifecycle_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用外层事务；独立 SQLite 写使用 IMMEDIATE 保证读后写原子性。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        if not is_postgres():
            own.execute("BEGIN IMMEDIATE")
        yield own


def _decode_json(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw or json.dumps(default))
    except (json.JSONDecodeError, TypeError):
        return default


def _event(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["evidence_refs"] = _decode_json(item.pop("evidence_refs_json", None), [])
    return item


def _action(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["metadata"] = _decode_json(item.pop("metadata_json", None), {})
    return item


def _clean_required(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _validate_actor(actor_type: str) -> str:
    cleaned = _clean_required(actor_type, "actor_type")
    if cleaned not in _ACTOR_TYPES:
        raise ValueError("invalid lifecycle actor_type")
    return cleaned


def _append_action(
    tx: Connection,
    *,
    event_id: str,
    action: str,
    actor_type: str,
    actor_id: Optional[str],
    metadata: Optional[Mapping[str, Any]],
    created_at: str,
) -> None:
    """在调用方事务中追加一条不可变 lifecycle action。"""
    tx.execute(
        """
        INSERT INTO resident_lifecycle_event_actions(
            id, event_id, action, actor_type, actor_id, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("lact"),
            event_id,
            _clean_required(action, "action"),
            _validate_actor(actor_type),
            str(actor_id).strip() if actor_id else None,
            json.dumps(
                dict(metadata or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            _clean_required(created_at, "created_at"),
        ),
    )


def _lock_resident_scope(
    tx: Connection,
    *,
    owner_platform_user_id: str,
    universe_id: str,
    resident_id: str,
) -> Dict[str, Any]:
    """按 owner→world→resident 顺序锁定并验证 lifecycle 隔离锚。"""
    suffix = " FOR UPDATE" if is_postgres() else ""
    world = tx.execute(
        "SELECT id FROM universes "
        "WHERE id = ? AND owner_platform_user_id = ? AND status = 'active'" + suffix,
        (universe_id, owner_platform_user_id),
    ).fetchone()
    if world is None:
        raise ValueError("lifecycle universe ownership mismatch")
    resident = tx.execute(
        "SELECT id, origin, status FROM universe_residents "
        "WHERE id = ? AND universe_id = ?" + suffix,
        (resident_id, universe_id),
    ).fetchone()
    if resident is None:
        raise ValueError("lifecycle resident ownership mismatch")
    return dict(resident)


def create_resident_lifecycle_event(
    *,
    owner_platform_user_id: str,
    universe_id: str,
    resident_id: str,
    event_type: str,
    status: str,
    policy_version: str,
    evidence_window_start: str,
    evidence_window_end: str,
    evidence_count: int,
    evidence_refs: Sequence[Mapping[str, Any]],
    cooldown_until: Optional[str],
    crisis_freeze_until: Optional[str],
    last_resident_exception_requested: bool,
    idempotency_key: str,
    request_fingerprint: str,
    actor_type: str,
    actor_id: Optional[str],
    created_at: str,
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """创建一个 owner/resident 隔离的 lifecycle event，幂等重放返回既有行。"""
    cleaned_type = _clean_required(event_type, "event_type")
    cleaned_status = _clean_required(status, "status")
    if cleaned_type not in _EVENT_TYPES:
        raise ValueError("invalid lifecycle event_type")
    if cleaned_status not in _OPEN_STATUSES:
        raise ValueError("invalid lifecycle initial status")
    if int(evidence_count) < 1:
        raise ValueError("evidence_count must be positive")
    cleaned_owner = _clean_required(owner_platform_user_id, "owner_platform_user_id")
    cleaned_universe = _clean_required(universe_id, "universe_id")
    cleaned_resident = _clean_required(resident_id, "resident_id")
    cleaned_key = _clean_required(idempotency_key, "idempotency_key")
    cleaned_fingerprint = _clean_required(request_fingerprint, "request_fingerprint")
    event_id = _new_id("life")
    evidence_json = json.dumps(
        list(evidence_refs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with _lifecycle_write_tx(conn) as tx:
        resident = _lock_resident_scope(
            tx,
            owner_platform_user_id=cleaned_owner,
            universe_id=cleaned_universe,
            resident_id=cleaned_resident,
        )
        if resident["origin"] == "legacy":
            raise ValueError("legacy resident departure forbidden")
        if resident["status"] != "active":
            raise ValueError("lifecycle resident is not active")

        existing = tx.execute(
            "SELECT * FROM resident_lifecycle_events WHERE idempotency_key = ?",
            (cleaned_key,),
        ).fetchone()
        if existing is not None:
            result = _event(existing)
            if (
                result["owner_platform_user_id"] != cleaned_owner
                or result["universe_id"] != cleaned_universe
                or result["resident_id"] != cleaned_resident
                or result["request_fingerprint"] != cleaned_fingerprint
            ):
                raise ValueError("lifecycle idempotency conflict")
            return result, False

        open_row = tx.execute(
            "SELECT id FROM resident_lifecycle_events "
            "WHERE resident_id = ? AND status IN ('cooling_down', 'review_pending')",
            (cleaned_resident,),
        ).fetchone()
        if open_row is not None:
            raise ValueError("lifecycle event already open")

        inserted = tx.execute(
            """
            INSERT INTO resident_lifecycle_events(
                id, owner_platform_user_id, universe_id, resident_id,
                event_type, status, policy_version, evidence_window_start,
                evidence_window_end, evidence_count, evidence_refs_json,
                cooldown_until, crisis_freeze_until,
                last_resident_exception_requested, idempotency_key,
                request_fingerprint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                event_id,
                cleaned_owner,
                cleaned_universe,
                cleaned_resident,
                cleaned_type,
                cleaned_status,
                _clean_required(policy_version, "policy_version"),
                _clean_required(evidence_window_start, "evidence_window_start"),
                _clean_required(evidence_window_end, "evidence_window_end"),
                int(evidence_count),
                evidence_json,
                str(cooldown_until).strip() if cooldown_until else None,
                str(crisis_freeze_until).strip() if crisis_freeze_until else None,
                1 if last_resident_exception_requested else 0,
                cleaned_key,
                cleaned_fingerprint,
                _clean_required(created_at, "created_at"),
                _clean_required(created_at, "created_at"),
            ),
        )
        if int(inserted.rowcount or 0) != 1:
            replay = tx.execute(
                "SELECT * FROM resident_lifecycle_events WHERE idempotency_key = ?",
                (cleaned_key,),
            ).fetchone()
            if replay is not None:
                result = _event(replay)
                if (
                    result["owner_platform_user_id"] != cleaned_owner
                    or result["universe_id"] != cleaned_universe
                    or result["resident_id"] != cleaned_resident
                    or result["request_fingerprint"] != cleaned_fingerprint
                ):
                    raise ValueError("lifecycle idempotency conflict")
                return result, False
            raise ValueError("lifecycle event already open")
        _append_action(
            tx,
            event_id=event_id,
            action="candidate_created",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"status": cleaned_status, "event_type": cleaned_type},
            created_at=created_at,
        )
        row = tx.execute(
            "SELECT * FROM resident_lifecycle_events WHERE id = ?", (event_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("lifecycle event insert failed")
    return _event(row), True


def get_resident_lifecycle_event(
    *, event_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """读取内部 lifecycle event；仅供后续 admin/platform adapter 使用。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM resident_lifecycle_events WHERE id = ?", (event_id,)
        ).fetchone()
    return _event(row) if row else None


def list_resident_lifecycle_scopes(
    *,
    after_resident_id: Optional[str] = None,
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """分页列出 confirmed world 的 active resident 评估锚，不读取任何聊天原文。"""
    params: List[Any] = []
    cursor_clause = ""
    if after_resident_id:
        cursor_clause = "AND r.id > ?"
        params.append(str(after_resident_id))
    params.append(max(1, min(int(limit), 500)))
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT
                r.id AS resident_id, r.universe_id, r.runtime_account_id,
                r.origin, r.status AS resident_status, r.joined_at,
                r.created_at AS resident_created_at,
                u.owner_platform_user_id, u.onboarding_state,
                c.id AS conversation_id, c.state AS conversation_state,
                e.id AS open_event_id, e.event_type AS open_event_type,
                e.status AS open_event_status, e.cooldown_until,
                e.evidence_window_end, e.crisis_freeze_until,
                (
                    SELECT m.id FROM messages m
                    WHERE m.account_id = r.runtime_account_id
                      AND m.direction = 'inbound' AND m.role = 'user'
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT 1
                ) AS last_inbound_message_id,
                (
                    SELECT m.created_at FROM messages m
                    WHERE m.account_id = r.runtime_account_id
                      AND m.direction = 'inbound' AND m.role = 'user'
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT 1
                ) AS last_inbound_at,
                (
                    SELECT COUNT(*) FROM universe_residents active_r
                    WHERE active_r.universe_id = r.universe_id
                      AND active_r.status = 'active'
                ) AS active_resident_count
            FROM universe_residents r
            JOIN universes u ON u.id = r.universe_id
            JOIN ai_conversations c ON c.resident_id = r.id
            LEFT JOIN resident_lifecycle_events e
              ON e.resident_id = r.id
             AND e.status IN ('cooling_down', 'review_pending')
            WHERE r.status = 'active'
              AND u.status = 'active'
              AND u.onboarding_state = 'confirmed'
              {cursor_clause}
            ORDER BY r.id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def list_resident_lifecycle_evidence_tasks(
    *,
    runtime_account_id: str,
    since: str,
    until: str,
    limit: int = 500,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """只返回目标 runtime 的结构化 moderation 字段，明确不选择 snapshot/raw。"""
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT id, source_type, source_id, message_db_id, status, risk_level,
                   risk_categories_json, confidence, policy_version,
                   metadata_json, created_at
            FROM content_moderation_tasks
            WHERE account_id = ?
              AND direction = 'inbound'
              AND created_at >= ?
              AND created_at <= ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (
                _clean_required(runtime_account_id, "runtime_account_id"),
                _clean_required(since, "since"),
                _clean_required(until, "until"),
                max(1, min(int(limit), 2000)),
            ),
        ).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["risk_categories"] = _decode_json(
            item.pop("risk_categories_json", None), []
        )
        item["metadata"] = _decode_json(item.pop("metadata_json", None), {})
        result.append(item)
    return result


def list_resident_lifecycle_events_for_review(
    *,
    statuses: Sequence[str] = ("review_pending",),
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出内部审核队列；不作为 owner API，调用方必须经过 admin 鉴权。"""
    cleaned = tuple(str(item).strip() for item in statuses if str(item).strip())
    if not cleaned:
        return []
    allowed = _OPEN_STATUSES | _TERMINAL_STATUSES | {"committed"}
    if any(item not in allowed for item in cleaned):
        raise ValueError("invalid lifecycle review status")
    placeholders = ",".join("?" for _ in cleaned)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT * FROM resident_lifecycle_events
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (*cleaned, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_event(row) for row in rows]


def list_resident_lifecycle_event_actions(
    *, event_id: str, conn: Optional[Connection] = None
) -> List[Dict[str, Any]]:
    """按事件读取 append-only action 审计链。"""
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT * FROM resident_lifecycle_event_actions "
            "WHERE event_id = ? ORDER BY created_at ASC, id ASC",
            (event_id,),
        ).fetchall()
    return [_action(row) for row in rows]


def transition_resident_lifecycle_event(
    *,
    event_id: str,
    expected_status: str,
    new_status: str,
    action: str,
    actor_type: str,
    actor_id: Optional[str],
    now: str,
    terminal_reason: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """CAS 推进非提交状态并追加审计；明确禁止写 ``committed``。"""
    expected = _clean_required(expected_status, "expected_status")
    target = _clean_required(new_status, "new_status")
    if target == "committed":
        raise ValueError("committed requires atomic offline transaction")
    if target not in _ALLOWED_TRANSITIONS.get(expected, set()):
        raise ValueError("invalid lifecycle transition")
    if target in _TERMINAL_STATUSES and not str(terminal_reason or "").strip():
        raise ValueError("terminal_reason is required")

    with _lifecycle_write_tx(conn) as tx:
        cursor = tx.execute(
            """
            UPDATE resident_lifecycle_events
            SET status = ?, terminal_reason = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                target,
                str(terminal_reason).strip() if terminal_reason else None,
                _clean_required(now, "now"),
                event_id,
                expected,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            return None
        _append_action(
            tx,
            event_id=event_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"from": expected, "to": target, **dict(metadata or {})},
            created_at=now,
        )
        row = tx.execute(
            "SELECT * FROM resident_lifecycle_events WHERE id = ?", (event_id,)
        ).fetchone()
    return _event(row) if row else None
