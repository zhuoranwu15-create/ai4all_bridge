"""M4 Companion World 私密 mailbox catalog/letter 存储原语。

所有 owner-facing 读取均以 ``owner_platform_user_id`` 为隔离锚。M4-1 只提供 catalog、
letter 写入/读取和非接受状态 CAS；``accepted`` 必须由 M4-5 的 runtime+resident+
conversation+letter 单事务完成。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._backend import Connection
from app.db._core import _new_id, _tx, connect

__all__ = [
    "count_unread_character_letters",
    "create_character_letter_catalog_entry",
    "expire_due_character_letters",
    "get_accepted_character_letter_resident",
    "get_character_letter_for_owner",
    "has_nonlegacy_resident_for_template",
    "insert_character_letter",
    "list_character_letter_catalog",
    "list_character_letters_for_owner",
    "list_mailbox_delivery_worlds",
    "lock_character_letter_accept_scope",
    "mark_locked_character_letter_accepted",
    "mark_locked_character_letter_expired",
    "prepare_character_letter_delivery",
    "retire_character_letter_catalog_entry",
    "transition_open_character_letter",
]

_CATALOG_STATUSES = {"active", "retired"}
_OPEN_LETTER_STATUSES = {"unread", "read", "deferred"}
_LETTER_TERMINAL_STATUSES = {"accepted", "declined", "expired"}
_STORAGE_TRANSITION_TARGETS = {"read", "deferred", "declined", "expired"}


@contextmanager
def _letter_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用调用方事务，或建立独立 PostgreSQL 写事务。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        yield own


def _clean_required(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _decode_json(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw or json.dumps(default))
    except (json.JSONDecodeError, TypeError):
        return default


def _catalog(row: Any) -> Dict[str, Any]:
    return dict(row)


def _letter(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["eligibility_snapshot"] = _decode_json(
        item.pop("eligibility_snapshot_json", None), {}
    )
    return item


def _lock_world_for_owner(
    tx: Connection, *, universe_id: str, owner_platform_user_id: str
) -> None:
    """按 owner→world 锚锁定 confirmed home world。"""
    suffix = " FOR UPDATE"
    row = tx.execute(
        "SELECT id FROM universes WHERE id = ? AND owner_platform_user_id = ? "
        "AND app_id = ? AND status = 'active' AND onboarding_state = 'confirmed'"
        + suffix,
        (universe_id, owner_platform_user_id, MINGCHAN_APP_ID),
    ).fetchone()
    if row is None:
        raise ValueError("mailbox universe ownership mismatch")


def create_character_letter_catalog_entry(
    *,
    character_key: str,
    character_template_id: str,
    template_version: str,
    letter_body: str,
    policy_version: str,
    priority: int,
    created_by: str,
    available_from: Optional[str] = None,
    available_until: Optional[str] = None,
    catalog_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """创建不可变 catalog 版本；相同 character/version 的完全重放返回既有行。"""
    cleaned_key = _clean_required(character_key, "character_key")
    cleaned_template = _clean_required(character_template_id, "character_template_id")
    cleaned_version = _clean_required(template_version, "template_version")
    cleaned_body = _clean_required(letter_body, "letter_body")
    cleaned_policy = _clean_required(policy_version, "policy_version")
    cleaned_actor = _clean_required(created_by, "created_by")
    resolved_id = str(catalog_id or _new_id("lcat")).strip()

    with _letter_write_tx(conn) as tx:
        template = tx.execute(
            "SELECT id, source_type, persona_version, status FROM character_templates "
            "WHERE id = ? AND app_id = ?",
            (cleaned_template, MINGCHAN_APP_ID),
        ).fetchone()
        if template is None:
            raise ValueError("letter catalog template not found")
        if template["status"] != "active" or template["source_type"] not in {
            "official",
            "operations",
        }:
            raise ValueError("letter catalog template unavailable")
        if str(template["persona_version"]) != cleaned_version:
            raise ValueError("letter catalog template version mismatch")

        existing = tx.execute(
            "SELECT * FROM character_letter_catalog "
            "WHERE character_key = ? AND template_version = ?",
            (cleaned_key, cleaned_version),
        ).fetchone()
        if existing is not None:
            result = _catalog(existing)
            expected = {
                "character_template_id": cleaned_template,
                "letter_body": cleaned_body,
                "policy_version": cleaned_policy,
                "priority": int(priority),
                "available_from": str(available_from).strip() if available_from else None,
                "available_until": str(available_until).strip() if available_until else None,
            }
            if any(result[field] != value for field, value in expected.items()):
                raise ValueError("letter catalog version conflict")
            return result, False

        tx.execute(
            """
            INSERT INTO character_letter_catalog(
                id, character_key, character_template_id, template_version,
                letter_body, policy_version, priority, status,
                available_from, available_until, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                resolved_id,
                cleaned_key,
                cleaned_template,
                cleaned_version,
                cleaned_body,
                cleaned_policy,
                int(priority),
                str(available_from).strip() if available_from else None,
                str(available_until).strip() if available_until else None,
                cleaned_actor,
            ),
        )
        row = tx.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ?", (resolved_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("letter catalog insert failed")
    return _catalog(row), True


def retire_character_letter_catalog_entry(
    *,
    catalog_id: str,
    retired_by: str,
    retired_at: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """幂等 retire catalog；不改写历史 letter 快照。"""
    with _letter_write_tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ?", (catalog_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "active":
            tx.execute(
                """
                UPDATE character_letter_catalog
                SET status = 'retired', retired_by = ?, retired_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (
                    _clean_required(retired_by, "retired_by"),
                    _clean_required(retired_at, "retired_at"),
                    _clean_required(retired_at, "retired_at"),
                    catalog_id,
                ),
            )
        result = tx.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ?", (catalog_id,)
        ).fetchone()
    return _catalog(result) if result else None


def list_character_letter_catalog(
    *,
    statuses: Sequence[str] = ("active",),
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出运营 catalog；调用方负责 admin 鉴权。"""
    cleaned = tuple(str(item).strip() for item in statuses if str(item).strip())
    if not cleaned:
        return []
    if any(item not in _CATALOG_STATUSES for item in cleaned):
        raise ValueError("invalid letter catalog status")
    placeholders = ",".join("?" for _ in cleaned)
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT c.* FROM character_letter_catalog c
            JOIN character_templates t ON t.id = c.character_template_id
            WHERE c.status IN ({placeholders}) AND t.app_id = ?
            ORDER BY c.priority DESC, c.id ASC
            LIMIT ?
            """,
            (*cleaned, MINGCHAN_APP_ID, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_catalog(row) for row in rows]


def list_mailbox_delivery_worlds(
    *,
    after_universe_id: Optional[str] = None,
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 universe id 稳定分页列出 active+confirmed world 投递锚。"""
    cursor_clause = ""
    params: List[Any] = [MINGCHAN_APP_ID]
    if after_universe_id:
        cursor_clause = "AND id > ?"
        params.append(str(after_universe_id))
    params.append(max(1, min(int(limit), 500)))
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT id AS universe_id, owner_platform_user_id
            FROM universes
            WHERE app_id = ? AND status = 'active' AND onboarding_state = 'confirmed'
              {cursor_clause}
            ORDER BY id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def expire_due_character_letters(
    *,
    now: str,
    owner_platform_user_id: Optional[str] = None,
    universe_id: Optional[str] = None,
    limit: Optional[int] = None,
    conn: Optional[Connection] = None,
) -> int:
    """CAS 过期 ``expires_at <= now`` 的 open letters，返回本次实际更新数。"""
    clauses = ["status IN ('unread', 'read', 'deferred')", "expires_at <= ?"]
    clean_now = _clean_required(now, "now")
    filter_params: List[Any] = [clean_now]
    if owner_platform_user_id is not None:
        clauses.append("owner_platform_user_id = ?")
        filter_params.append(
            _clean_required(owner_platform_user_id, "owner_platform_user_id")
        )
    if universe_id is not None:
        clauses.append("universe_id = ?")
        filter_params.append(_clean_required(universe_id, "universe_id"))
    limit_clause = ""
    if limit is not None:
        limit_clause = " LIMIT ?"
        filter_params.append(max(1, min(int(limit), 2000)))
    with _letter_write_tx(conn) as tx:
        cursor = tx.execute(
            f"""
            UPDATE character_letters
            SET status = 'expired', handled_at = ?, terminal_reason = 'ttl_expired',
                updated_at = ?
            WHERE id IN (
                SELECT id FROM character_letters
                WHERE {' AND '.join(clauses)}
                ORDER BY expires_at ASC, id ASC{limit_clause}
            )
              AND status IN ('unread', 'read', 'deferred')
              AND expires_at <= ?
            """,
            (clean_now, clean_now, *filter_params, clean_now),
        )
        wish_letter_clauses = ["source = 'wish'", "status = 'expired'"]
        wish_letter_params: List[Any] = []
        if owner_platform_user_id is not None:
            wish_letter_clauses.append("owner_platform_user_id = ?")
            wish_letter_params.append(owner_platform_user_id)
        if universe_id is not None:
            wish_letter_clauses.append("universe_id = ?")
            wish_letter_params.append(universe_id)
        tx.execute(
            f"""
            UPDATE resident_wishes
            SET closed_at = COALESCE(closed_at, ?),
                terminal_reason = COALESCE(terminal_reason, 'letter_expired'),
                wish_text = NULL, updated_at = ?
            WHERE closed_at IS NULL AND letter_id IN (
                SELECT id FROM character_letters
                WHERE {' AND '.join(wish_letter_clauses)}
            )
            """,
            (clean_now, clean_now, *wish_letter_params),
        )
    return int(cursor.rowcount or 0)


def prepare_character_letter_delivery(
    *,
    universe_id: str,
    now: str,
    cooldown_since: str,
    active_limit: int,
    conn: Connection,
) -> Dict[str, Any]:
    """锁定 world、过期旧信并重算投递资格，返回确定性 catalog 候选。"""
    suffix = " FOR UPDATE"
    world = conn.execute(
        "SELECT * FROM universes WHERE id = ? AND app_id = ?" + suffix,
        (_clean_required(universe_id, "universe_id"), MINGCHAN_APP_ID),
    ).fetchone()
    if (
        world is None
        or world["status"] != "active"
        or world["onboarding_state"] != "confirmed"
    ):
        return {"status": "world_ineligible", "expired": 0}
    expired = expire_due_character_letters(
        now=now,
        universe_id=universe_id,
        conn=conn,
    )
    active_row = conn.execute(
        "SELECT COUNT(*) AS c FROM universe_residents "
        "WHERE universe_id = ? AND status = 'active'",
        (universe_id,),
    ).fetchone()
    active_count = int(active_row["c"] if active_row else 0)
    base = {
        "owner_platform_user_id": str(world["owner_platform_user_id"]),
        "universe_id": str(world["id"]),
        "active_count": active_count,
        "expired": expired,
    }
    if active_count >= int(active_limit):
        return {**base, "status": "blocked_capacity"}
    open_row = conn.execute(
        "SELECT id FROM character_letters WHERE universe_id = ? "
        "AND source = 'organic' AND status IN ('unread', 'read', 'deferred') LIMIT 1",
        (universe_id,),
    ).fetchone()
    if open_row is not None:
        return {**base, "status": "blocked_open"}
    last_row = conn.execute(
        "SELECT delivered_at FROM character_letters WHERE universe_id = ? AND source = 'organic' "
        "ORDER BY delivered_at DESC, id DESC LIMIT 1",
        (universe_id,),
    ).fetchone()
    if last_row is not None and str(last_row["delivered_at"]) > str(cooldown_since):
        return {
            **base,
            "status": "blocked_cooldown",
            "last_delivered_at": str(last_row["delivered_at"]),
        }
    catalog_lock = " FOR UPDATE OF c"
    catalog = conn.execute(
        """
        SELECT c.*
        FROM character_letter_catalog c
        JOIN character_templates t ON t.id = c.character_template_id
        WHERE c.status = 'active'
          AND c.source = 'organic'
          AND (c.available_from IS NULL OR c.available_from <= ?)
          AND (c.available_until IS NULL OR c.available_until > ?)
          AND t.status = 'active'
          AND t.source_type IN ('official', 'operations')
          AND t.persona_version = c.template_version
          AND NOT EXISTS (
              SELECT 1 FROM character_letters used
              WHERE used.universe_id = ? AND used.character_key = c.character_key
          )
          AND NOT EXISTS (
              SELECT 1 FROM universe_residents resident
              WHERE resident.universe_id = ? AND resident.origin <> 'legacy'
                AND (
                    resident.character_template_id = c.character_template_id
                    OR EXISTS (
                        SELECT 1 FROM character_letter_catalog known_version
                        WHERE known_version.character_template_id = resident.character_template_id
                          AND known_version.character_key = c.character_key
                    )
                )
          )
        ORDER BY c.priority DESC, c.id ASC
        LIMIT 1
        """
        + catalog_lock,
        (now, now, universe_id, universe_id),
    ).fetchone()
    if catalog is None:
        return {**base, "status": "catalog_empty"}
    return {**base, "status": "eligible", "catalog": _catalog(catalog)}


def insert_character_letter(
    *,
    owner_platform_user_id: str,
    universe_id: str,
    catalog_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    eligibility_snapshot: Mapping[str, Any],
    policy_version: str,
    delivered_at: str,
    expires_at: str,
    source: str = "organic",
    wish_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """写入一封快照 letter；容量/cooldown eligibility 由 M4-4 服务在同 world 锁内编排。"""
    cleaned_owner = _clean_required(owner_platform_user_id, "owner_platform_user_id")
    cleaned_universe = _clean_required(universe_id, "universe_id")
    cleaned_catalog = _clean_required(catalog_id, "catalog_id")
    cleaned_key = _clean_required(idempotency_key, "idempotency_key")
    cleaned_fingerprint = _clean_required(request_fingerprint, "request_fingerprint")
    delivered = _clean_required(delivered_at, "delivered_at")
    expires = _clean_required(expires_at, "expires_at")
    clean_source = str(source or "organic").strip()
    if clean_source not in {"organic", "wish"}:
        raise ValueError("invalid letter source")
    if (clean_source == "wish") != bool(wish_id):
        raise ValueError("wish letter source/id mismatch")
    if expires <= delivered:
        raise ValueError("letter expires_at must be after delivered_at")
    letter_id = _new_id("letter")

    with _letter_write_tx(conn) as tx:
        _lock_world_for_owner(
            tx,
            universe_id=cleaned_universe,
            owner_platform_user_id=cleaned_owner,
        )
        existing = tx.execute(
            "SELECT * FROM character_letters WHERE idempotency_key = ?",
            (cleaned_key,),
        ).fetchone()
        if existing is not None:
            result = _letter(existing)
            if (
                result["owner_platform_user_id"] != cleaned_owner
                or result["universe_id"] != cleaned_universe
                or result["catalog_id"] != cleaned_catalog
                or result["request_fingerprint"] != cleaned_fingerprint
                or str(result.get("source") or "organic") != clean_source
                or result.get("wish_id") != wish_id
            ):
                raise ValueError("letter idempotency conflict")
            return result, False

        catalog = tx.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ? AND status = 'active'",
            (cleaned_catalog,),
        ).fetchone()
        if catalog is None:
            raise ValueError("letter catalog unavailable")
        if str(catalog["source"] or "organic") != clean_source:
            raise ValueError("letter/catalog source mismatch")
        if catalog["available_from"] and str(catalog["available_from"]) > delivered:
            raise ValueError("letter catalog not started")
        if catalog["available_until"] and str(catalog["available_until"]) <= delivered:
            raise ValueError("letter catalog expired")

        tx.execute(
            """
            INSERT INTO character_letters(
                id, owner_platform_user_id, universe_id, catalog_id,
                character_key, character_template_id, template_version,
                body_text, status, idempotency_key, request_fingerprint,
                eligibility_snapshot_json, policy_version, delivered_at, expires_at,
                source, wish_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                letter_id,
                cleaned_owner,
                cleaned_universe,
                cleaned_catalog,
                catalog["character_key"],
                catalog["character_template_id"],
                catalog["template_version"],
                catalog["letter_body"],
                cleaned_key,
                cleaned_fingerprint,
                json.dumps(
                    dict(eligibility_snapshot),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                _clean_required(policy_version, "policy_version"),
                delivered,
                expires,
                clean_source,
                wish_id,
            ),
        )
        row = tx.execute(
            "SELECT * FROM character_letters WHERE id = ?", (letter_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("character letter insert failed")
    return _letter(row), True


def get_character_letter_for_owner(
    *,
    letter_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 letter id + owner 读取；越权与不存在统一返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT l.*, t.name AS character_name, t.avatar_ref, t.summary, t.tags_json
            FROM character_letters l
            JOIN character_templates t ON t.id = l.character_template_id
            WHERE l.id = ? AND l.owner_platform_user_id = ?
            """,
            (letter_id, owner_platform_user_id),
        ).fetchone()
    return _letter(row) if row else None


def lock_character_letter_accept_scope(
    *,
    letter_id: str,
    owner_platform_user_id: str,
    conn: Connection,
) -> Optional[Dict[str, Any]]:
    """按 ``world → letter/catalog/template`` 顺序锁定一次 owner accept 的事实快照。"""
    suffix = " FOR UPDATE"
    world = conn.execute(
        "SELECT * FROM universes WHERE owner_platform_user_id = ? AND app_id = ? "
        "AND status = 'active' AND onboarding_state = 'confirmed'" + suffix,
        (owner_platform_user_id, MINGCHAN_APP_ID),
    ).fetchone()
    if world is None:
        return None
    row_lock = " FOR UPDATE OF l, c, t"
    row = conn.execute(
        """
        SELECT l.*,
               c.status AS catalog_status,
               c.character_key AS catalog_character_key,
               c.character_template_id AS catalog_character_template_id,
               c.template_version AS catalog_template_version,
               c.source AS catalog_source,
               t.status AS template_status,
               t.source_type AS template_source_type,
               t.persona_version AS current_template_version,
               t.persona_seed_json,
               t.owner_platform_user_id AS template_owner_platform_user_id,
               t.name AS character_name,
               t.avatar_ref,
               t.summary,
               t.tags_json
        FROM character_letters l
        JOIN character_letter_catalog c ON c.id = l.catalog_id
        JOIN character_templates t ON t.id = l.character_template_id
        WHERE l.id = ? AND l.owner_platform_user_id = ? AND l.universe_id = ?
        """
        + row_lock,
        (letter_id, owner_platform_user_id, world["id"]),
    ).fetchone()
    return _letter(row) if row else None


def has_nonlegacy_resident_for_template(
    *, universe_id: str, character_template_id: str, conn: Connection
) -> bool:
    """返回本 world 是否已有相同模板的 non-legacy 关系。"""
    row = conn.execute(
        "SELECT 1 FROM universe_residents WHERE universe_id = ? "
        "AND character_template_id = ? AND origin <> 'legacy' LIMIT 1",
        (universe_id, character_template_id),
    ).fetchone()
    return row is not None


def mark_locked_character_letter_expired(
    *, letter_id: str, owner_platform_user_id: str, now: str, conn: Connection
) -> bool:
    """把已锁定且到期的 open letter CAS 为 expired。"""
    cursor = conn.execute(
        """
        UPDATE character_letters
        SET status = 'expired', handled_at = ?, terminal_reason = 'ttl_expired',
            updated_at = ?
        WHERE id = ? AND owner_platform_user_id = ?
          AND status IN ('unread', 'read', 'deferred') AND expires_at <= ?
        """,
        (now, now, letter_id, owner_platform_user_id, now),
    )
    changed = int(cursor.rowcount or 0) == 1
    if changed:
        conn.execute(
            """
            UPDATE resident_wishes
            SET closed_at = COALESCE(closed_at, ?), terminal_reason = 'letter_expired',
                wish_text = NULL, updated_at = ?
            WHERE letter_id = ? AND closed_at IS NULL
            """,
            (now, now, letter_id),
        )
    return changed


def mark_locked_character_letter_accepted(
    *,
    letter_id: str,
    owner_platform_user_id: str,
    resident_id: str,
    now: str,
    conn: Connection,
) -> bool:
    """把已锁定的 open letter CAS 为 accepted，并钉住创建出的 resident。"""
    cursor = conn.execute(
        """
        UPDATE character_letters
        SET status = 'accepted', accepted_resident_id = ?, handled_at = ?,
            terminal_reason = NULL, updated_at = ?
        WHERE id = ? AND owner_platform_user_id = ?
          AND status IN ('unread', 'read', 'deferred') AND expires_at > ?
        """,
        (resident_id, now, now, letter_id, owner_platform_user_id, now),
    )
    changed = int(cursor.rowcount or 0) == 1
    if changed:
        conn.execute(
            """
            UPDATE resident_wishes
            SET closed_at = COALESCE(closed_at, ?), terminal_reason = 'letter_accepted',
                wish_text = NULL, updated_at = ?
            WHERE letter_id = ? AND closed_at IS NULL
            """,
            (now, now, letter_id),
        )
    return changed


def get_accepted_character_letter_resident(
    *, letter_id: str, owner_platform_user_id: str, conn: Connection
) -> Optional[Dict[str, Any]]:
    """读取 accepted letter 对应的公开 resident/conversation 投影。"""
    row = conn.execute(
        """
        SELECT r.id AS resident_id, r.origin, r.status,
               COALESCE(p.display_name, t.name) AS name, t.avatar_ref,
               c.id AS conversation_id, c.state AS conversation_state
        FROM character_letters l
        JOIN universe_residents r ON r.id = l.accepted_resident_id
        JOIN character_templates t ON t.id = r.character_template_id
        LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
        JOIN ai_conversations c ON c.resident_id = r.id
        WHERE l.id = ? AND l.owner_platform_user_id = ? AND l.status = 'accepted'
        """,
        (letter_id, owner_platform_user_id),
    ).fetchone()
    return dict(row) if row else None


def list_character_letters_for_owner(
    *,
    owner_platform_user_id: str,
    statuses: Optional[Sequence[str]] = None,
    cursor_delivered_at: Optional[str] = None,
    cursor_letter_id: Optional[str] = None,
    limit: int = 21,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 owner + `(delivered_at,id)` tuple cursor 列出 mailbox 历史。"""
    if bool(cursor_delivered_at) != bool(cursor_letter_id):
        raise ValueError("invalid_cursor")
    clauses = ["l.owner_platform_user_id = ?"]
    params: List[Any] = [owner_platform_user_id]
    if statuses is not None:
        cleaned = tuple(str(item).strip() for item in statuses if str(item).strip())
        allowed = _OPEN_LETTER_STATUSES | _LETTER_TERMINAL_STATUSES
        if not cleaned or any(item not in allowed for item in cleaned):
            raise ValueError("invalid letter status")
        placeholders = ",".join("?" for _ in cleaned)
        clauses.append(f"l.status IN ({placeholders})")
        params.extend(cleaned)
    if cursor_delivered_at and cursor_letter_id:
        clauses.append(
            "(l.delivered_at < ? OR (l.delivered_at = ? AND l.id < ?))"
        )
        params.extend([cursor_delivered_at, cursor_delivered_at, cursor_letter_id])
    params.append(max(1, min(int(limit), 101)))
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT l.*, t.name AS character_name, t.avatar_ref, t.summary, t.tags_json
            FROM character_letters l
            JOIN character_templates t ON t.id = l.character_template_id
            WHERE {' AND '.join(clauses)}
            ORDER BY l.delivered_at DESC, l.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_letter(row) for row in rows]


def count_unread_character_letters(
    *,
    owner_platform_user_id: str,
    now: str,
    conn: Optional[Connection] = None,
) -> int:
    """按 owner 统计尚未逻辑过期的 unread letters。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT COUNT(*) AS c FROM character_letters "
            "WHERE owner_platform_user_id = ? AND status = 'unread' AND expires_at > ?",
            (owner_platform_user_id, now),
        ).fetchone()
    return int(row["c"] if row else 0)


def transition_open_character_letter(
    *,
    letter_id: str,
    owner_platform_user_id: str,
    new_status: str,
    now: str,
    terminal_reason: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """推进 read/defer/decline/expire；明确禁止通过本原语写 accepted。"""
    target = _clean_required(new_status, "new_status")
    if target == "accepted":
        raise ValueError("accepted requires atomic resident transaction")
    if target not in _STORAGE_TRANSITION_TARGETS:
        raise ValueError("invalid letter transition target")
    if target in {"declined", "expired"} and not str(terminal_reason or "").strip():
        raise ValueError("terminal_reason is required")

    with _letter_write_tx(conn) as tx:
        suffix = " FOR UPDATE"
        current = tx.execute(
            "SELECT * FROM character_letters "
            "WHERE id = ? AND owner_platform_user_id = ?" + suffix,
            (letter_id, owner_platform_user_id),
        ).fetchone()
        if current is None:
            return None
        current_status = str(current["status"])
        if current_status == target:
            return _letter(current)
        if current_status not in _OPEN_LETTER_STATUSES:
            return None
        if target == "read" and current_status != "unread":
            return _letter(current)

        read_at = now if target == "read" else current["read_at"]
        deferred_at = now if target == "deferred" else current["deferred_at"]
        handled_at = now if target in {"declined", "expired"} else None
        cursor = tx.execute(
            """
            UPDATE character_letters
            SET status = ?, read_at = ?, deferred_at = ?, handled_at = ?,
                terminal_reason = ?, updated_at = ?
            WHERE id = ? AND owner_platform_user_id = ?
              AND status IN ('unread', 'read', 'deferred')
            """,
            (
                target,
                read_at,
                deferred_at,
                handled_at,
                str(terminal_reason).strip() if terminal_reason else None,
                _clean_required(now, "now"),
                letter_id,
                owner_platform_user_id,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            return None
        if target in {"declined", "expired"}:
            tx.execute(
                """
                UPDATE resident_wishes
                SET closed_at = COALESCE(closed_at, ?), terminal_reason = ?,
                    wish_text = NULL, updated_at = ?
                WHERE letter_id = ? AND closed_at IS NULL
                """,
                (
                    now,
                    "letter_declined" if target == "declined" else "letter_expired",
                    now,
                    letter_id,
                ),
            )
        row = tx.execute(
            "SELECT * FROM character_letters WHERE id = ? AND owner_platform_user_id = ?",
            (letter_id, owner_platform_user_id),
        ).fetchone()
    return _letter(row) if row else None
