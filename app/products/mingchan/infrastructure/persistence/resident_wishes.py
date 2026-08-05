"""异步居民许愿的 owner-scoped 持久化与持久任务原语。"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Tuple

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect
from .companion_world import create_character_template

__all__ = [
    "claim_resident_wish_job",
    "complete_resident_wish_generation",
    "create_resident_wish",
    "deliver_resident_wish",
    "fail_resident_wish_job",
    "get_current_resident_wish",
    "get_resident_wish_by_request",
    "get_resident_wish_for_owner",
    "withdraw_resident_wish",
]


def _decode_json(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def _wish(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["input_safety"] = _decode_json(item.pop("input_safety_json", None), {})
    return item


def _job(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["generation"] = _decode_json(item.pop("generation_json", None), None)
    item["safety"] = _decode_json(item.pop("safety_json", None), None)
    return item


def get_resident_wish_by_request(
    *, owner_platform_user_id: str, client_request_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """按 owner + client request 读取；幂等键绝不跨真人共享。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT w.* FROM resident_wishes w JOIN universes u ON u.id=w.universe_id "
            "WHERE w.owner_platform_user_id = ? AND w.client_request_id = ? "
            "AND u.app_id = ?",
            (owner_platform_user_id, client_request_id, MINGCHAN_APP_ID),
        ).fetchone()
    return _wish(row) if row else None


def get_resident_wish_for_owner(
    *, wish_id: str, owner_platform_user_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """按 wish id + owner 读取；越权与不存在统一返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT w.* FROM resident_wishes w JOIN universes u ON u.id=w.universe_id "
            "WHERE w.id = ? AND w.owner_platform_user_id = ? AND u.app_id = ?",
            (wish_id, owner_platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    return _wish(row) if row else None


def get_current_resident_wish(
    *, owner_platform_user_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """返回 owner 最近一笔愿望，包含已闭环终态供客户端恢复结果。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT w.* FROM resident_wishes w JOIN universes u ON u.id=w.universe_id "
            "WHERE w.owner_platform_user_id = ? AND u.app_id = ? "
            "ORDER BY w.submitted_at DESC, w.id DESC LIMIT 1",
            (owner_platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    return _wish(row) if row else None


def create_resident_wish(
    *,
    owner_platform_user_id: str,
    client_request_id: str,
    request_fingerprint: str,
    wish_text: str,
    input_safety: Mapping[str, Any],
    submitted_at: str,
    deliver_not_before: str,
    deliver_by: str,
    daily_window_start: Optional[str],
    daily_max: int,
) -> Tuple[Dict[str, Any], bool]:
    """单事务受理 wish + durable job；返回 ``(wish, created)``。"""
    with connect() as tx:
        if not is_postgres():
            tx.execute("BEGIN IMMEDIATE")
        lock = " FOR UPDATE OF u, p" if is_postgres() else ""
        scope = tx.execute(
            """
            SELECT u.*, p.status AS owner_status
            FROM universes u
            JOIN platform_users p ON p.id = u.owner_platform_user_id
            WHERE u.owner_platform_user_id = ? AND u.app_id = ?
            """
            + lock,
            (owner_platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
        if (
            scope is None
            or scope["owner_status"] != "active"
            or scope["status"] != "active"
            or scope["onboarding_state"] != "confirmed"
        ):
            raise ValueError("world_not_ready")

        existing = tx.execute(
            "SELECT * FROM resident_wishes "
            "WHERE owner_platform_user_id = ? AND universe_id = ? "
            "AND client_request_id = ?",
            (owner_platform_user_id, scope["id"], client_request_id),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise ValueError("idempotency_conflict")
            return _wish(existing), False

        capacity = tx.execute(
            "SELECT COUNT(*) AS c FROM universe_residents "
            "WHERE universe_id = ? AND status = 'active'",
            (scope["id"],),
        ).fetchone()
        if int(capacity["c"] if capacity else 0) >= 10:
            raise ValueError("resident_capacity_exceeded")
        open_wish = tx.execute(
            "SELECT id FROM resident_wishes "
            "WHERE owner_platform_user_id = ? AND universe_id = ? "
            "AND closed_at IS NULL LIMIT 1",
            (owner_platform_user_id, scope["id"]),
        ).fetchone()
        if open_wish is not None:
            raise ValueError("wish_already_pending")
        if daily_max > 0 and daily_window_start:
            count = tx.execute(
                "SELECT COUNT(*) AS c FROM resident_wishes "
                "WHERE owner_platform_user_id = ? AND universe_id = ? "
                "AND submitted_at >= ?",
                (owner_platform_user_id, scope["id"], daily_window_start),
            ).fetchone()
            if int(count["c"] if count else 0) >= daily_max:
                raise ValueError("wish_rate_limited")

        wish_id = _new_id("wish")
        job_id = _new_id("wishjob")
        tx.execute(
            """
            INSERT INTO resident_wishes(
                id, owner_platform_user_id, universe_id, client_request_id,
                request_fingerprint, wish_text, input_safety_json, status,
                submitted_at, deliver_not_before, deliver_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                wish_id,
                owner_platform_user_id,
                scope["id"],
                client_request_id,
                request_fingerprint,
                wish_text,
                json.dumps(dict(input_safety), ensure_ascii=False, sort_keys=True),
                submitted_at,
                deliver_not_before,
                deliver_by,
            ),
        )
        tx.execute(
            "INSERT INTO resident_wish_jobs(id, wish_id, status, next_attempt_at) "
            "VALUES (?, ?, 'queued', ?)",
            (job_id, wish_id, submitted_at),
        )
        row = tx.execute("SELECT * FROM resident_wishes WHERE id = ?", (wish_id,)).fetchone()
    if row is None:
        raise RuntimeError("resident wish insert failed")
    return _wish(row), True


def withdraw_resident_wish(
    *, wish_id: str, owner_platform_user_id: str, now: str
) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str]]:
    """原子收回；返回 ``(wish, replayed, error_code)``。"""
    with connect() as tx:
        if not is_postgres():
            tx.execute("BEGIN IMMEDIATE")
        suffix = " FOR UPDATE" if is_postgres() else ""
        row = tx.execute(
            "SELECT w.* FROM resident_wishes w "
            "JOIN universes u ON u.id = w.universe_id "
            "WHERE w.id = ? AND w.owner_platform_user_id = ? AND u.app_id = ?"
            + suffix,
            (wish_id, owner_platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
        if row is None:
            return None, False, "wish_not_found"
        if row["status"] == "withdrawn":
            return _wish(row), True, None
        if row["status"] != "pending" or row["closed_at"] is not None:
            return _wish(row), False, "wish_not_withdrawable"
        changed = tx.execute(
            """
            UPDATE resident_wishes
            SET status = 'withdrawn', closed_at = ?, terminal_reason = 'owner_withdrew',
                wish_text = NULL, updated_at = ?
            WHERE id = ? AND owner_platform_user_id = ?
              AND status = 'pending' AND closed_at IS NULL
            """,
            (now, now, wish_id, owner_platform_user_id),
        )
        if int(changed.rowcount or 0) != 1:
            current = tx.execute("SELECT * FROM resident_wishes WHERE id = ?", (wish_id,)).fetchone()
            return (_wish(current) if current else None), False, "wish_not_withdrawable"
        tx.execute(
            """
            UPDATE resident_wish_jobs
            SET status = 'cancelled', claim_token = NULL, lease_expires_at = NULL,
                completed_at = ?, updated_at = ?
            WHERE wish_id = ? AND status <> 'completed'
            """,
            (now, now, wish_id),
        )
        updated = tx.execute("SELECT * FROM resident_wishes WHERE id = ?", (wish_id,)).fetchone()
    return (_wish(updated) if updated else None), False, None


def claim_resident_wish_job(
    *, now: str, lease_expires_at: str, claim_token: str
) -> Optional[Dict[str, Any]]:
    """跨进程 claim 一笔到期 job；PG 使用 SKIP LOCKED，SQLite 用 IMMEDIATE 串行化。"""
    with connect() as tx:
        if not is_postgres():
            tx.execute("BEGIN IMMEDIATE")
        lock = " FOR UPDATE OF j, w SKIP LOCKED" if is_postgres() else ""
        row = tx.execute(
            """
            SELECT j.id AS job_id, j.wish_id
            FROM resident_wish_jobs j
            JOIN resident_wishes w ON w.id = j.wish_id
            JOIN universes u ON u.id = w.universe_id
            WHERE w.status = 'pending' AND w.closed_at IS NULL AND u.app_id = ?
              AND j.next_attempt_at <= ?
              AND (
                    j.status IN ('queued', 'ready')
                    OR (j.status = 'running' AND j.lease_expires_at <= ?)
                  )
            ORDER BY j.next_attempt_at ASC, j.id ASC
            LIMIT 1
            """
            + lock,
            (MINGCHAN_APP_ID, now, now),
        ).fetchone()
        if row is None:
            return None
        changed = tx.execute(
            """
            UPDATE resident_wish_jobs
            SET status = 'running', attempt_count = attempt_count + 1,
                claim_token = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ?
              AND (status IN ('queued', 'ready') OR lease_expires_at <= ?)
            """,
            (claim_token, lease_expires_at, now, row["job_id"], now),
        )
        if int(changed.rowcount or 0) != 1:
            return None
        joined = tx.execute(
            """
            SELECT j.*, w.owner_platform_user_id, w.universe_id, w.wish_text,
                   w.status AS wish_status, w.submitted_at, w.deliver_not_before,
                   w.deliver_by, w.closed_at, u.status AS universe_status,
                   u.onboarding_state, p.status AS owner_status
            FROM resident_wish_jobs j
            JOIN resident_wishes w ON w.id = j.wish_id
            JOIN universes u ON u.id = w.universe_id
            JOIN platform_users p ON p.id = w.owner_platform_user_id
            WHERE j.id = ? AND j.claim_token = ? AND u.app_id = ?
            """,
            (row["job_id"], claim_token, MINGCHAN_APP_ID),
        ).fetchone()
    return _job(joined) if joined else None


def complete_resident_wish_generation(
    *,
    wish_id: str,
    claim_token: str,
    generation: Mapping[str, Any],
    safety: Mapping[str, Any],
    next_attempt_at: str,
    now: str,
) -> bool:
    """保存受控生成物并把任务排到最早投递时间；不保存模型原始响应。"""
    with connect() as tx:
        changed = tx.execute(
            """
            UPDATE resident_wish_jobs
            SET status = 'ready', generation_json = ?, safety_json = ?,
                next_attempt_at = ?, claim_token = NULL, lease_expires_at = NULL,
                last_error_code = NULL, updated_at = ?
            WHERE wish_id = ? AND status = 'running' AND claim_token = ?
              AND EXISTS (
                  SELECT 1 FROM resident_wishes w
                  JOIN universes u ON u.id = w.universe_id
                  WHERE w.id = resident_wish_jobs.wish_id AND u.app_id = ?
                    AND w.status = 'pending' AND w.closed_at IS NULL
              )
            """,
            (
                json.dumps(dict(generation), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(safety), ensure_ascii=False, sort_keys=True),
                next_attempt_at,
                now,
                wish_id,
                claim_token,
                MINGCHAN_APP_ID,
            ),
        )
    return int(changed.rowcount or 0) == 1


def fail_resident_wish_job(
    *,
    wish_id: str,
    claim_token: str,
    error_code: str,
    now: str,
    next_attempt_at: str,
) -> str:
    """安全地重排失败任务；跨过 72h 截止线时原子转 ``unfulfilled``。"""
    with connect() as tx:
        if not is_postgres():
            tx.execute("BEGIN IMMEDIATE")
        suffix = " FOR UPDATE OF w, j" if is_postgres() else ""
        row = tx.execute(
            """
            SELECT w.*, j.status AS job_status, j.claim_token AS job_claim_token
            FROM resident_wishes w
            JOIN resident_wish_jobs j ON j.wish_id = w.id
            JOIN universes u ON u.id = w.universe_id
            WHERE w.id = ? AND u.app_id = ?
            """
            + suffix,
            (wish_id, MINGCHAN_APP_ID),
        ).fetchone()
        if row is None or row["status"] != "pending" or row["closed_at"] is not None:
            return "cancelled"
        if row["job_status"] != "running" or row["job_claim_token"] != claim_token:
            return "claim_lost"
        if now >= str(row["deliver_by"]) or next_attempt_at >= str(row["deliver_by"]):
            tx.execute(
                """
                UPDATE resident_wishes
                SET status = 'unfulfilled', closed_at = ?, terminal_reason = ?,
                    wish_text = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending' AND closed_at IS NULL
                """,
                (now, error_code, now, wish_id),
            )
            tx.execute(
                """
                UPDATE resident_wish_jobs
                SET status = 'dead', last_error_code = ?, claim_token = NULL,
                    lease_expires_at = NULL, completed_at = ?, updated_at = ?
                WHERE wish_id = ? AND claim_token = ?
                """,
                (error_code, now, now, wish_id, claim_token),
            )
            return "unfulfilled"
        tx.execute(
            """
            UPDATE resident_wish_jobs
            SET status = 'queued', next_attempt_at = ?, last_error_code = ?,
                claim_token = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE wish_id = ? AND claim_token = ?
            """,
            (next_attempt_at, error_code, now, wish_id, claim_token),
        )
    return "queued"


def deliver_resident_wish(
    *,
    wish_id: str,
    claim_token: str,
    generation: Mapping[str, Any],
    now: str,
    letter_expires_at: str,
) -> Dict[str, Any]:
    """单事务创建生成模板/唯一来信并完成 wish/job，保证 exactly-once 投递。"""
    with connect() as tx:
        if not is_postgres():
            tx.execute("BEGIN IMMEDIATE")
        lock = " FOR UPDATE OF w, j, u, p" if is_postgres() else ""
        row = tx.execute(
            """
            SELECT w.*, j.status AS job_status, j.claim_token AS job_claim_token,
                   u.status AS universe_status, u.onboarding_state,
                   p.status AS owner_status
            FROM resident_wishes w
            JOIN resident_wish_jobs j ON j.wish_id = w.id
            JOIN universes u ON u.id = w.universe_id
            JOIN platform_users p ON p.id = w.owner_platform_user_id
            WHERE w.id = ? AND u.app_id = ?
            """
            + lock,
            (wish_id, MINGCHAN_APP_ID),
        ).fetchone()
        if row is None:
            return {"status": "cancelled"}
        if row["status"] == "delivered" and row["letter_id"]:
            return {"status": "delivered", "letter_id": str(row["letter_id"]), "replayed": True}
        if (
            row["status"] != "pending"
            or row["closed_at"] is not None
            or row["job_status"] != "running"
            or row["job_claim_token"] != claim_token
        ):
            return {"status": "cancelled"}
        if now < str(row["deliver_not_before"]):
            raise ValueError("wish_delivery_too_early")
        if now > str(row["deliver_by"]):
            tx.execute(
                "UPDATE resident_wishes SET status='unfulfilled', closed_at=?, "
                "terminal_reason='delivery_window_elapsed', wish_text=NULL, updated_at=? "
                "WHERE id=?",
                (now, now, wish_id),
            )
            tx.execute(
                "UPDATE resident_wish_jobs SET status='dead', completed_at=?, "
                "claim_token=NULL, lease_expires_at=NULL, updated_at=? WHERE wish_id=?",
                (now, now, wish_id),
            )
            return {"status": "unfulfilled"}
        if (
            row["owner_status"] != "active"
            or row["universe_status"] != "active"
            or row["onboarding_state"] != "confirmed"
        ):
            tx.execute(
                "UPDATE resident_wishes SET status='unfulfilled', closed_at=?, "
                "terminal_reason='account_unavailable', wish_text=NULL, updated_at=? "
                "WHERE id=?",
                (now, now, wish_id),
            )
            tx.execute(
                "UPDATE resident_wish_jobs SET status='cancelled', completed_at=?, "
                "claim_token=NULL, lease_expires_at=NULL, updated_at=? WHERE wish_id=?",
                (now, now, wish_id),
            )
            return {"status": "unfulfilled"}

        template_id = f"tmpl_{wish_id}"
        catalog_id = f"lcat_{wish_id}"
        character_key = f"wish:{wish_id}"
        version = "wish_v1"
        create_character_template(
            template_id=template_id,
            app_id=MINGCHAN_APP_ID,
            source_type="generated",
            owner_platform_user_id=str(row["owner_platform_user_id"]),
            name=str(generation["name"]),
            avatar_ref=str(generation["avatar_ref"]),
            summary=str(generation["normalized_summary"]),
            long_summary=str(generation["normalized_summary"]),
            tags_json=json.dumps(list(generation["tags"]), ensure_ascii=False),
            persona_seed_json=json.dumps(generation["persona_seed"], ensure_ascii=False),
            persona_version=version,
            relationship_type=str(generation["relationship_type"]),
            personality_traits_json=json.dumps(
                list(generation["personality_traits"]), ensure_ascii=False
            ),
            conn=tx,
        )
        tx.execute(
            """
            INSERT INTO character_letter_catalog(
                id, character_key, character_template_id, template_version,
                letter_body, policy_version, priority, status, source, created_by
            ) VALUES (?, ?, ?, ?, ?, 'resident_wish_v1', 0, 'active', 'wish', 'resident_wish_worker')
            """,
            (
                catalog_id,
                character_key,
                template_id,
                version,
                str(generation["letter_body"]),
            ),
        )
        letter_id = _new_id("letter")
        fingerprint = hashlib.sha256(
            json.dumps(dict(generation), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        tx.execute(
            """
            INSERT INTO character_letters(
                id, owner_platform_user_id, universe_id, catalog_id,
                character_key, character_template_id, template_version,
                body_text, status, idempotency_key, request_fingerprint,
                eligibility_snapshot_json, policy_version, delivered_at, expires_at,
                source, wish_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?,
                      'resident_wish_v1', ?, ?, 'wish', ?)
            """,
            (
                letter_id,
                row["owner_platform_user_id"],
                row["universe_id"],
                catalog_id,
                character_key,
                template_id,
                version,
                str(generation["letter_body"]),
                f"resident-wish:v1:{wish_id}",
                fingerprint,
                json.dumps({"source": "wish", "wish_id": wish_id}, sort_keys=True),
                now,
                letter_expires_at,
                wish_id,
            ),
        )
        changed = tx.execute(
            """
            UPDATE resident_wishes
            SET status = 'delivered', letter_id = ?, wish_text = NULL, updated_at = ?
            WHERE id = ? AND status = 'pending' AND closed_at IS NULL
            """,
            (letter_id, now, wish_id),
        )
        if int(changed.rowcount or 0) != 1:
            raise RuntimeError("resident wish delivery CAS failed")
        tx.execute(
            """
            UPDATE resident_wish_jobs
            SET status = 'completed', completed_at = ?, claim_token = NULL,
                lease_expires_at = NULL, last_error_code = NULL, updated_at = ?
            WHERE wish_id = ? AND status = 'running' AND claim_token = ?
            """,
            (now, now, wish_id, claim_token),
        )
    return {"status": "delivered", "letter_id": letter_id, "replayed": False}
