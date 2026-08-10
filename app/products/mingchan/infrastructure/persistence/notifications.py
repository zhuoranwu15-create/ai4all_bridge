"""M3 App 拉取式通知数据原语。

所有公开读取和写入都以 ``platform_user_id`` 为首要锚；``universe_id`` 与
``resident_id`` 只能作为已验证的从属资源，禁止用 runtime ``account_id`` 代替真人隔离。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._backend import Connection
from app.db._core import _new_id, _tx, connect

__all__ = [
    "cleanup_app_notifications_batch",
    "count_unread_app_notifications",
    "cancel_human_app_notification",
    "finalize_human_app_notification",
    "get_app_notification_for_owner",
    "insert_visible_app_notification",
    "list_app_notifications",
    "mark_all_app_notifications_read",
    "mark_app_notification_read",
    "reserve_human_app_notification",
    "reserve_human_app_notification_observed",
]


@contextmanager
def _notification_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用调用方事务，或建立独立 PostgreSQL 通知写事务。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        yield own


def get_app_notification_for_owner(
    *,
    notification_id: str,
    platform_user_id: str,
    app_id: str = MINGCHAN_APP_ID,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按通知 id + platform owner 读取；越权与不存在统一返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT n.*, COALESCE(p.display_name, t.name) AS resident_name,
                   t.avatar_ref AS resident_avatar_ref
            FROM app_notifications n
            LEFT JOIN universe_residents r
              ON r.id = n.resident_id AND r.universe_id = n.universe_id
            LEFT JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
            WHERE n.id = ? AND n.platform_user_id = ? AND n.app_id = ?
            """,
            (notification_id, platform_user_id, app_id),
        ).fetchone()
    return dict(row) if row else None


def _lock_notification_owner(tx: Connection, platform_user_id: str) -> None:
    """按全局锁序锁定真人；不存在时拒绝继续写入。"""
    lock_suffix = " FOR UPDATE"
    owner = tx.execute(
        "SELECT id FROM platform_users WHERE id = ?" + lock_suffix,
        (platform_user_id,),
    ).fetchone()
    if owner is None:
        raise ValueError("platform user not found")


def _validate_visible_target(
    tx: Connection,
    *,
    platform_user_id: str,
    universe_id: str,
    resident_id: Optional[str],
    app_id: str,
) -> None:
    """校验通知从属 world/resident，禁止跨真人或跨 universe 写入。"""
    world = tx.execute(
        """
        SELECT id FROM universes
        WHERE id = ? AND owner_platform_user_id = ? AND app_id = ?
          AND status = 'active' AND onboarding_state = 'confirmed'
        """,
        (universe_id, platform_user_id, app_id),
    ).fetchone()
    if world is None:
        raise ValueError("universe ownership mismatch")
    if resident_id is None:
        return
    resident = tx.execute(
        """
        SELECT id FROM universe_residents
        WHERE id = ? AND universe_id = ? AND status = 'active'
        """,
        (resident_id, universe_id),
    ).fetchone()
    if resident is None:
        raise ValueError("resident ownership mismatch")


def _delete_notification_ids(
    tx: Connection, *, platform_user_id: str, app_id: str, ids: List[str]
) -> int:
    """显式带 owner predicate 删除通知 ID 集合。"""
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = tx.execute(
        f"DELETE FROM app_notifications WHERE platform_user_id = ? AND app_id = ? "
        f"AND id IN ({placeholders})",
        (platform_user_id, app_id, *ids),
    )
    return int(cursor.rowcount or 0)


def _enforce_visible_limit(
    tx: Connection,
    *,
    platform_user_id: str,
    app_id: str,
    now: str,
    max_visible: int,
    protected_notification_id: Optional[str] = None,
) -> int:
    """真人锁内删除逻辑过期行，并按 read→unread 顺序维持 visible 上限。"""
    deleted = int(
        tx.execute(
            """
            DELETE FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ?
              AND delivery_status IN ('visible', 'cancelled')
              AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (platform_user_id, app_id, now),
        ).rowcount
        or 0
    )
    count = int(
        tx.execute(
            """
            SELECT COUNT(*) AS c FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND delivery_status = 'visible'
              AND expires_at > ?
            """,
            (platform_user_id, app_id, now),
        ).fetchone()["c"]
    )
    overflow = max(0, count - max(1, int(max_visible)))
    if overflow <= 0:
        return deleted
    protected_clause = "AND id <> ?" if protected_notification_id else ""
    protected_params: List[Any] = (
        [protected_notification_id] if protected_notification_id else []
    )
    read_rows = tx.execute(
        f"""
        SELECT id FROM app_notifications
        WHERE platform_user_id = ? AND app_id = ? AND delivery_status = 'visible'
          AND expires_at > ? AND read_at IS NOT NULL
          {protected_clause}
        ORDER BY read_at ASC, delivered_at ASC, id ASC
        LIMIT ?
        """,
        (platform_user_id, app_id, now, *protected_params, overflow),
    ).fetchall()
    read_ids = [str(row["id"]) for row in read_rows]
    deleted += _delete_notification_ids(
        tx, platform_user_id=platform_user_id, app_id=app_id, ids=read_ids
    )
    overflow -= len(read_ids)
    if overflow <= 0:
        return deleted
    unread_rows = tx.execute(
        f"""
        SELECT id FROM app_notifications
        WHERE platform_user_id = ? AND app_id = ? AND delivery_status = 'visible'
          AND expires_at > ? AND read_at IS NULL
          {protected_clause}
        ORDER BY delivered_at ASC, id ASC
        LIMIT ?
        """,
        (platform_user_id, app_id, now, *protected_params, overflow),
    ).fetchall()
    deleted += _delete_notification_ids(
        tx,
        platform_user_id=platform_user_id,
        app_id=app_id,
        ids=[str(row["id"]) for row in unread_rows],
    )
    return deleted


def insert_visible_app_notification(
    *,
    platform_user_id: str,
    app_id: str = MINGCHAN_APP_ID,
    universe_id: str,
    resident_id: Optional[str],
    scope: str,
    category: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    request_fingerprint: str,
    title: Optional[str],
    body_text: str,
    target_type: str,
    target_id: Optional[str],
    delivered_at: str,
    expires_at: str,
    now: str,
    metadata: Optional[Dict[str, Any]] = None,
    max_visible: int = 200,
    conn: Optional[Connection] = None,
) -> Tuple[Dict[str, Any], bool]:
    """直接写一条 post-policy visible 通知，并在同事务内维持 owner 上限。"""
    if scope not in {"resident", "human"}:
        raise ValueError("invalid notification scope")
    if scope == "resident" and not resident_id:
        raise ValueError("resident scope requires resident_id")
    if target_type not in {"none", "conversation", "feed"}:
        raise ValueError("invalid notification target_type")
    if target_type == "none" and target_id is not None:
        raise ValueError("none target cannot have target_id")
    if target_type != "none" and not str(target_id or "").strip():
        raise ValueError("target_id is required")
    if not str(body_text or "").strip():
        raise ValueError("body_text is required")
    if not str(idempotency_key or "").strip() or not str(
        request_fingerprint or ""
    ).strip():
        raise ValueError("notification idempotency fields are required")
    metadata_json = json.dumps(
        metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    notification_id = _new_id("notif")
    with _notification_write_tx(conn) as tx:
        _lock_notification_owner(tx, platform_user_id)
        _validate_visible_target(
            tx,
            platform_user_id=platform_user_id,
            universe_id=universe_id,
            resident_id=resident_id,
            app_id=app_id,
        )
        existing = tx.execute(
            """
            SELECT * FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND idempotency_key = ?
            """,
            (platform_user_id, app_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            result = dict(existing)
            if str(result["request_fingerprint"]) != str(request_fingerprint):
                raise ValueError("notification idempotency conflict")
            if result["delivery_status"] != "visible":
                raise ValueError("notification idempotency state conflict")
            _enforce_visible_limit(
                tx,
                platform_user_id=platform_user_id,
                app_id=app_id,
                now=now,
                max_visible=max_visible,
            )
            return result, False
        tx.execute(
            """
            INSERT INTO app_notifications(
                id, platform_user_id, app_id, universe_id, resident_id, scope, category,
                source_type, source_id, idempotency_key, request_fingerprint,
                delivery_status, title, body_text, target_type, target_id,
                metadata_json, delivered_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'visible', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification_id,
                platform_user_id,
                app_id,
                universe_id,
                resident_id,
                scope,
                category,
                source_type,
                source_id,
                idempotency_key,
                request_fingerprint,
                title,
                str(body_text).strip(),
                target_type,
                target_id,
                metadata_json,
                delivered_at,
                expires_at,
            ),
        )
        _enforce_visible_limit(
            tx,
            platform_user_id=platform_user_id,
            app_id=app_id,
            now=now,
            max_visible=max_visible,
            protected_notification_id=notification_id,
        )
        row = tx.execute(
            "SELECT * FROM app_notifications "
            "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
            (notification_id, platform_user_id, app_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("newest visible notification was pruned")
    return dict(row), True


def list_app_notifications(
    *,
    platform_user_id: str,
    now: str,
    app_id: str = MINGCHAN_APP_ID,
    unread_only: bool = False,
    cursor_delivered_at: Optional[str] = None,
    cursor_notification_id: Optional[str] = None,
    limit: int = 21,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """按 owner + tuple cursor 列出未逻辑过期 visible 通知。"""
    if bool(cursor_delivered_at) != bool(cursor_notification_id):
        raise ValueError("invalid_cursor")
    clauses = [
        "n.platform_user_id = ?",
        "n.app_id = ?",
        "n.delivery_status = 'visible'",
        "n.expires_at > ?",
    ]
    params: List[Any] = [platform_user_id, app_id, now]
    if unread_only:
        clauses.append("n.read_at IS NULL")
    if cursor_delivered_at and cursor_notification_id:
        clauses.append(
            "(n.delivered_at < ? OR (n.delivered_at = ? AND n.id < ?))"
        )
        params.extend(
            [cursor_delivered_at, cursor_delivered_at, cursor_notification_id]
        )
    params.append(max(1, min(int(limit), 51)))
    with _tx(conn) as tx:
        rows = tx.execute(
            f"""
            SELECT n.*, COALESCE(p.display_name, t.name) AS resident_name,
                   t.avatar_ref AS resident_avatar_ref
            FROM app_notifications n
            LEFT JOIN universe_residents r
              ON r.id = n.resident_id AND r.universe_id = n.universe_id
            LEFT JOIN character_templates t ON t.id = r.character_template_id
            LEFT JOIN profiles p ON p.account_id = r.runtime_account_id
            WHERE {' AND '.join(clauses)}
            ORDER BY n.delivered_at DESC, n.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def count_unread_app_notifications(
    *,
    platform_user_id: str,
    now: str,
    app_id: str = MINGCHAN_APP_ID,
    conn: Optional[Connection] = None,
) -> int:
    """统计 owner 当前有效 visible unread；Feed 不进入该计数。"""
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT COUNT(*) AS c FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND delivery_status = 'visible'
              AND read_at IS NULL AND expires_at > ?
            """,
            (platform_user_id, app_id, now),
        ).fetchone()
    return int(row["c"] if row else 0)


def mark_app_notification_read(
    *,
    notification_id: str,
    platform_user_id: str,
    app_id: str = MINGCHAN_APP_ID,
    now: str,
    read_expires_at: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """owner 条件 CAS 单条已读；重复调用不延长首次 read 的 7 天期限。"""
    with _notification_write_tx(conn) as tx:
        tx.execute(
            """
            UPDATE app_notifications
            SET read_at = ?, expires_at = ?,
                updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id = ? AND platform_user_id = ? AND app_id = ?
              AND delivery_status = 'visible'
              AND expires_at > ? AND read_at IS NULL
            """,
            (now, read_expires_at, notification_id, platform_user_id, app_id, now),
        )
        row = tx.execute(
            """
            SELECT * FROM app_notifications
            WHERE id = ? AND platform_user_id = ? AND app_id = ?
              AND delivery_status = 'visible'
              AND expires_at > ?
            """,
            (notification_id, platform_user_id, app_id, now),
        ).fetchone()
    return dict(row) if row else None


def mark_all_app_notifications_read(
    *,
    platform_user_id: str,
    now: str,
    read_expires_at: str,
    app_id: str = MINGCHAN_APP_ID,
    conn: Optional[Connection] = None,
) -> Tuple[int, str]:
    """真人锁内线性化 read-all；锁释放后新插入的通知保持 unread。"""
    with _notification_write_tx(conn) as tx:
        _lock_notification_owner(tx, platform_user_id)
        cursor = tx.execute(
            """
            UPDATE app_notifications
            SET read_at = ?, expires_at = ?,
                updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id = ? AND app_id = ? AND delivery_status = 'visible'
              AND expires_at > ? AND read_at IS NULL
            """,
            (now, read_expires_at, platform_user_id, app_id, now),
        )
    return int(cursor.rowcount or 0), now


def cleanup_app_notifications_batch(
    *, now: str, limit: int = 100, max_visible: int = 200
) -> Dict[str, Any]:
    """执行一批 central-only reservation/TTL/上限维护；单 owner 失败隔离。"""
    clean_limit = max(1, min(int(limit), 1000))
    cancelled_expires_at = (
        datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(days=7)
    ).strftime("%Y-%m-%d %H:%M:%S")
    cancelled = 0
    deleted = 0
    lock_suffix = " FOR UPDATE SKIP LOCKED"
    with _notification_write_tx(None) as tx:
        rows = tx.execute(
            """
            SELECT id FROM app_notifications
            WHERE app_id = ? AND delivery_status = 'reserved' AND claim_expires_at <= ?
            ORDER BY claim_expires_at ASC, id ASC LIMIT ?
            """
            + lock_suffix,
            (MINGCHAN_APP_ID, now, clean_limit),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = tx.execute(
                f"""
                UPDATE app_notifications
                SET delivery_status = 'cancelled', cancelled_at = ?, expires_at = ?,
                    terminal_reason = 'claim_expired', claim_token = NULL,
                    updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE app_id = ? AND delivery_status = 'reserved' AND claim_expires_at <= ?
                  AND id IN ({placeholders})
                """,
                (now, cancelled_expires_at, MINGCHAN_APP_ID, now, *ids),
            )
            cancelled = int(cursor.rowcount or 0)
    with _notification_write_tx(None) as tx:
        rows = tx.execute(
            """
            SELECT id, platform_user_id FROM app_notifications
            WHERE app_id = ? AND delivery_status IN ('visible', 'cancelled')
              AND expires_at IS NOT NULL AND expires_at <= ?
            ORDER BY expires_at ASC, id ASC LIMIT ?
            """
            + lock_suffix,
            (MINGCHAN_APP_ID, now, clean_limit),
        ).fetchall()
        by_owner: Dict[str, List[str]] = {}
        for row in rows:
            by_owner.setdefault(str(row["platform_user_id"]), []).append(str(row["id"]))
        for owner_id, ids in by_owner.items():
            deleted += _delete_notification_ids(
                tx,
                platform_user_id=owner_id,
                app_id=MINGCHAN_APP_ID,
                ids=ids,
            )
    with _tx(None) as tx:
        owners = tx.execute(
            """
            SELECT platform_user_id FROM app_notifications
            WHERE app_id = ? AND delivery_status = 'visible' AND expires_at > ?
            GROUP BY platform_user_id HAVING COUNT(*) > ?
            ORDER BY platform_user_id ASC LIMIT ?
            """,
            (MINGCHAN_APP_ID, now, max(1, int(max_visible)), clean_limit),
        ).fetchall()
    reconciled = 0
    errors: Dict[str, str] = {}
    for row in owners:
        owner_id = str(row["platform_user_id"])
        try:
            with _notification_write_tx(None) as tx:
                _lock_notification_owner(tx, owner_id)
                deleted += _enforce_visible_limit(
                    tx,
                    platform_user_id=owner_id,
                    app_id=MINGCHAN_APP_ID,
                    now=now,
                    max_visible=max_visible,
                )
            reconciled += 1
        except Exception as err:  # noqa: BLE001 — 单真人维护失败不阻断后续 owner
            errors[owner_id] = str(err)
    return {
        "cancelled_reservations": cancelled,
        "deleted": deleted,
        "reconciled_users": reconciled,
        "errors": errors or None,
    }


def reserve_human_app_notification(
    *,
    platform_user_id: str,
    universe_id: str,
    category: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    request_fingerprint: str,
    claim_token: str,
    claim_expires_at: str,
    now: str,
    visible_since: str,
    metadata: Optional[Dict[str, Any]] = None,
    conn: Optional[Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """为真人级 App-only 触达原子预留隐藏通知，返回 ``(row, acquired)``。

    先锁 platform user，再检查滚动 24 小时 visible 与未过期 reservation；因此同一
    真人的多个 resident/due worker 不会各自占一条。
    同幂等键同 fingerprint 返回原行，异 fingerprint fail-closed。
    """
    if not str(idempotency_key or "").strip():
        raise ValueError("idempotency_key is required")
    if not str(request_fingerprint or "").strip():
        raise ValueError("request_fingerprint is required")
    metadata_json = json.dumps(
        metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    try:
        cancelled_expires_at = (
            datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(days=7)
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError as err:
        raise ValueError("now must be Beijing naive database time") from err
    with _notification_write_tx(conn) as tx:
        lock_suffix = " FOR UPDATE"
        owner = tx.execute(
            "SELECT id FROM platform_users WHERE id = ?" + lock_suffix,
            (platform_user_id,),
        ).fetchone()
        if owner is None:
            raise ValueError("platform user not found")
        world = tx.execute(
            """
            SELECT id FROM universes
            WHERE id = ? AND owner_platform_user_id = ?
              AND app_id = ?
              AND status = 'active' AND onboarding_state = 'confirmed'
            """,
            (universe_id, platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
        if world is None:
            raise ValueError("universe ownership mismatch")

        # 过期 reservation 立即退出 live 集合；物理删除仍由 central cleanup 执行。
        tx.execute(
            """
            UPDATE app_notifications
            SET delivery_status = 'cancelled', cancelled_at = ?, expires_at = ?,
                terminal_reason = 'claim_expired', claim_token = NULL,
                updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id = ? AND app_id = ? AND scope = 'human'
              AND delivery_status = 'reserved' AND claim_expires_at <= ?
            """,
            (now, cancelled_expires_at, platform_user_id, MINGCHAN_APP_ID, now),
        )

        existing = tx.execute(
            """
            SELECT * FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND idempotency_key = ?
            """,
            (platform_user_id, MINGCHAN_APP_ID, idempotency_key),
        ).fetchone()
        if existing is not None:
            result = dict(existing)
            if str(result["request_fingerprint"]) != str(request_fingerprint):
                raise ValueError("notification idempotency conflict")
            if result["delivery_status"] == "cancelled":
                live = tx.execute(
                    """
                    SELECT id FROM app_notifications
                    WHERE platform_user_id = ? AND app_id = ?
                      AND scope = 'human' AND id <> ?
                      AND (
                            (delivery_status = 'visible' AND delivered_at > ?)
                         OR (delivery_status = 'reserved' AND claim_expires_at > ?)
                      )
                    LIMIT 1
                    """,
                    (
                        platform_user_id,
                        MINGCHAN_APP_ID,
                        result["id"],
                        visible_since,
                        now,
                    ),
                ).fetchone()
                if live is not None:
                    return None, False
                tx.execute(
                    """
                    UPDATE app_notifications
                    SET delivery_status = 'reserved', metadata_json = ?,
                        claim_token = ?, claim_expires_at = ?, cancelled_at = NULL,
                        terminal_reason = NULL, expires_at = NULL,
                        updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE id = ? AND platform_user_id = ? AND app_id = ?
                      AND delivery_status = 'cancelled'
                    """,
                    (
                        metadata_json,
                        claim_token,
                        claim_expires_at,
                        result["id"],
                        platform_user_id,
                        MINGCHAN_APP_ID,
                    ),
                )
                reacquired = tx.execute(
                    "SELECT * FROM app_notifications "
                    "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
                    (result["id"], platform_user_id, MINGCHAN_APP_ID),
                ).fetchone()
                if reacquired is None:
                    raise RuntimeError("notification reservation disappeared")
                return dict(reacquired), True
            return result, False

        live = tx.execute(
            """
            SELECT id FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND scope = 'human'
              AND (
                    (delivery_status = 'visible' AND delivered_at > ?)
                 OR (delivery_status = 'reserved' AND claim_expires_at > ?)
              )
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (platform_user_id, MINGCHAN_APP_ID, visible_since, now),
        ).fetchone()
        if live is not None:
            return None, False

        notification_id = _new_id("notif")
        tx.execute(
            """
            INSERT INTO app_notifications(
                id, platform_user_id, app_id, universe_id, scope, category,
                source_type, source_id, idempotency_key, request_fingerprint,
                delivery_status, metadata_json, claim_token, claim_expires_at
            )
            VALUES (?, ?, ?, ?, 'human', ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
            """,
            (
                notification_id,
                platform_user_id,
                MINGCHAN_APP_ID,
                universe_id,
                category,
                source_type,
                source_id,
                idempotency_key,
                request_fingerprint,
                metadata_json,
                claim_token,
                claim_expires_at,
            ),
        )
        row = tx.execute(
            "SELECT * FROM app_notifications "
            "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
            (notification_id, platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    if row is None:
        raise RuntimeError("notification reservation was not created")
    return dict(row), True


def reserve_human_app_notification_observed(
    **kwargs: Any,
) -> Tuple[Optional[Dict[str, Any]], bool, str]:
    """执行真人级 reservation，并返回低基数裁决原因供 heartbeat 聚合。"""

    row, acquired = reserve_human_app_notification(**kwargs)
    if acquired:
        return row, True, "claimed"
    if row is not None:
        return row, False, f"idempotent_{row['delivery_status']}"

    platform_user_id = str(kwargs["platform_user_id"])
    visible_since = str(kwargs["visible_since"])
    now = str(kwargs["now"])
    conn = kwargs.get("conn")
    with _tx(conn) as tx:
        live = tx.execute(
            """
            SELECT delivery_status
            FROM app_notifications
            WHERE platform_user_id = ? AND app_id = ? AND scope = 'human'
              AND (
                    (delivery_status = 'visible' AND delivered_at > ?)
                 OR (delivery_status = 'reserved' AND claim_expires_at > ?)
              )
            ORDER BY CASE WHEN delivery_status = 'visible' THEN 0 ELSE 1 END,
                     created_at ASC, id ASC
            LIMIT 1
            """,
            (platform_user_id, MINGCHAN_APP_ID, visible_since, now),
        ).fetchone()
    if live is None:
        return None, False, "not_claimed"
    return (
        None,
        False,
        (
            "blocked_24h"
            if live["delivery_status"] == "visible"
            else "blocked_inflight"
        ),
    )


def cancel_human_app_notification(
    *,
    notification_id: str,
    platform_user_id: str,
    claim_token: str,
    reason: str,
    now: str,
    expires_at: str,
    conn: Optional[Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """仅当前 reservation token 可取消；旧 worker 不得覆盖重领后的新 lease。"""

    with _notification_write_tx(conn) as tx:
        _lock_notification_owner(tx, platform_user_id)
        cursor = tx.execute(
            """
            UPDATE app_notifications
            SET delivery_status = 'cancelled', cancelled_at = ?, expires_at = ?,
                terminal_reason = ?, claim_token = NULL, claim_expires_at = NULL,
                updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id = ? AND platform_user_id = ? AND app_id = ? AND scope = 'human'
              AND delivery_status = 'reserved' AND claim_token = ?
            """,
            (
                now,
                expires_at,
                str(reason or "cancelled")[:120],
                notification_id,
                platform_user_id,
                MINGCHAN_APP_ID,
                claim_token,
            ),
        )
        row = tx.execute(
            "SELECT * FROM app_notifications "
            "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
            (notification_id, platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    return (dict(row) if row else None), int(cursor.rowcount or 0) > 0


def finalize_human_app_notification(
    *,
    notification_id: str,
    platform_user_id: str,
    universe_id: str,
    claim_token: str,
    expected_resident_id: str,
    allow_speaker_reselection: bool,
    title: Optional[str],
    body_text: str,
    target_type: str,
    target_id: Optional[str],
    delivered_at: str,
    expires_at: str,
    now: str,
    max_visible: int = 200,
    conn: Optional[Connection] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """锁 owner→最终 speaker 后把当前 token 的 hidden reservation CAS 为 visible。"""

    if not str(body_text or "").strip():
        raise ValueError("body_text is required")
    if target_type not in {"none", "conversation", "feed"}:
        raise ValueError("invalid notification target_type")
    if target_type == "none" and target_id is not None:
        raise ValueError("none target cannot have target_id")
    if target_type != "none" and not str(target_id or "").strip():
        raise ValueError("target_id is required")

    from . import companion_world as world_db

    with _notification_write_tx(conn) as tx:
        _lock_notification_owner(tx, platform_user_id)
        reservation = tx.execute(
            "SELECT * FROM app_notifications "
            "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
            (notification_id, platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
        if (
            reservation is None
            or reservation["delivery_status"] != "reserved"
            or reservation["claim_token"] != claim_token
            or not reservation["claim_expires_at"]
            or reservation["claim_expires_at"] <= now
        ):
            return (dict(reservation) if reservation else None), False

        speaker: Optional[Dict[str, Any]]
        if allow_speaker_reselection:
            speaker = world_db.select_human_app_speaker(
                platform_user_id=platform_user_id,
                universe_id=universe_id,
                lock_resident=True,
                conn=tx,
            )
        else:
            # 只锁 resident，避免 JOIN 查询顺带锁 universe/conversation 打乱全局锁序。
            lock_suffix = " FOR UPDATE OF r"
            row = tx.execute(
                """
                SELECT r.id AS resident_id, r.runtime_account_id,
                       c.id AS conversation_id
                FROM universe_residents r
                JOIN universes u ON u.id = r.universe_id
                JOIN ai_conversations c ON c.resident_id = r.id
                WHERE r.id = ? AND r.universe_id = ?
                  AND u.owner_platform_user_id = ?
                  AND u.app_id = ?
                  AND u.status = 'active' AND u.onboarding_state = 'confirmed'
                  AND r.status = 'active' AND r.runtime_account_id IS NOT NULL
                """ + lock_suffix,
                (
                    expected_resident_id,
                    universe_id,
                    platform_user_id,
                    MINGCHAN_APP_ID,
                ),
            ).fetchone()
            speaker = dict(row) if row else None

        if speaker is None:
            cancelled_expires_at = (
                datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(days=7)
            ).strftime("%Y-%m-%d %H:%M:%S")
            tx.execute(
                """
                UPDATE app_notifications
                SET delivery_status = 'cancelled', cancelled_at = ?, expires_at = ?,
                    terminal_reason = 'speaker_unavailable', claim_token = NULL,
                    claim_expires_at = NULL,
                    updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id = ? AND platform_user_id = ? AND app_id = ?
                  AND delivery_status = 'reserved'
                  AND claim_token = ?
                """,
                (
                    now,
                    cancelled_expires_at,
                    notification_id,
                    platform_user_id,
                    MINGCHAN_APP_ID,
                    claim_token,
                ),
            )
            row = tx.execute(
                "SELECT * FROM app_notifications "
                "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
                (notification_id, platform_user_id, MINGCHAN_APP_ID),
            ).fetchone()
            return (dict(row) if row else None), False

        effective_target_id = target_id
        if target_type == "conversation":
            # speaker 允许重选时 target 必须随最终 resident 一起重算，不能指向旧会话。
            effective_target_id = str(speaker["conversation_id"])
        cursor = tx.execute(
            """
            UPDATE app_notifications
            SET resident_id = ?, delivery_status = 'visible', title = ?, body_text = ?,
                target_type = ?, target_id = ?, delivered_at = ?, expires_at = ?,
                claim_token = NULL, claim_expires_at = NULL, cancelled_at = NULL,
                terminal_reason = NULL,
                updated_at = to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id = ? AND platform_user_id = ? AND app_id = ? AND universe_id = ?
              AND scope = 'human' AND delivery_status = 'reserved'
              AND claim_token = ? AND claim_expires_at > ?
            """,
            (
                speaker["resident_id"],
                title,
                str(body_text).strip(),
                target_type,
                effective_target_id,
                delivered_at,
                expires_at,
                notification_id,
                platform_user_id,
                MINGCHAN_APP_ID,
                universe_id,
                claim_token,
                now,
            ),
        )
        finalized = int(cursor.rowcount or 0) > 0
        if finalized:
            _enforce_visible_limit(
                tx,
                platform_user_id=platform_user_id,
                app_id=MINGCHAN_APP_ID,
                now=now,
                max_visible=max_visible,
                protected_notification_id=notification_id,
            )
        row = tx.execute(
            "SELECT * FROM app_notifications "
            "WHERE id = ? AND platform_user_id = ? AND app_id = ?",
            (notification_id, platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    return (dict(row) if row else None), finalized
