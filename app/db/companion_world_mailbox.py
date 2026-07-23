"""M4 Companion World 私密 mailbox catalog/letter 存储原语。

所有 owner-facing 读取均以 ``owner_platform_user_id`` 为隔离锚。M4-1 只提供 catalog、
letter 写入/读取和非接受状态 CAS；``accepted`` 必须由 M4-5 的 runtime+resident+
conversation+letter 单事务完成。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "count_unread_character_letters",
    "create_character_letter_catalog_entry",
    "get_character_letter_for_owner",
    "insert_character_letter",
    "list_character_letter_catalog",
    "list_character_letters_for_owner",
    "retire_character_letter_catalog_entry",
    "transition_open_character_letter",
]

_CATALOG_STATUSES = {"active", "retired"}
_OPEN_LETTER_STATUSES = {"unread", "read", "deferred"}
_LETTER_TERMINAL_STATUSES = {"accepted", "declined", "expired"}
_STORAGE_TRANSITION_TARGETS = {"read", "deferred", "declined", "expired"}


@contextmanager
def _letter_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用外层事务；独立 SQLite 写使用 IMMEDIATE 串行读后写。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        if not is_postgres():
            own.execute("BEGIN IMMEDIATE")
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
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = tx.execute(
        "SELECT id FROM universes WHERE id = ? AND owner_platform_user_id = ? "
        "AND status = 'active' AND onboarding_state = 'confirmed'" + suffix,
        (universe_id, owner_platform_user_id),
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
            "WHERE id = ?",
            (cleaned_template,),
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
            SELECT * FROM character_letter_catalog
            WHERE status IN ({placeholders})
            ORDER BY priority DESC, id ASC
            LIMIT ?
            """,
            (*cleaned, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_catalog(row) for row in rows]


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
            ):
                raise ValueError("letter idempotency conflict")
            return result, False

        catalog = tx.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ? AND status = 'active'",
            (cleaned_catalog,),
        ).fetchone()
        if catalog is None:
            raise ValueError("letter catalog unavailable")
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
                eligibility_snapshot_json, policy_version, delivered_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?, ?, ?, ?)
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
        suffix = " FOR UPDATE" if is_postgres() else ""
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
        row = tx.execute(
            "SELECT * FROM character_letters WHERE id = ? AND owner_platform_user_id = ?",
            (letter_id, owner_platform_user_id),
        ).fetchone()
    return _letter(row) if row else None
