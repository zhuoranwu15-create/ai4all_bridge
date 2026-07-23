"""M5 Companion World invite/visit 存储原语。

本模块只提供 owner/visitor 锚定的底层操作，不注册 API。容量与跨实体状态机由后续
``app.platform`` 事务编排再次校验；所有可选 ``conn`` 都可加入同一外层事务。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

from app.db._backend import Connection, is_postgres
from app.db._core import _new_id, _tx, connect

__all__ = [
    "count_open_universe_visits_for_visitor",
    "ensure_universe_visit_slots",
    "get_universe_invite_by_code_hash",
    "get_universe_invite_for_owner",
    "get_universe_visit",
    "get_universe_visit_for_participant",
    "insert_pending_universe_visit",
    "insert_universe_invite",
    "lock_platform_user_for_visit",
    "lock_universe_visit",
    "list_universe_invites_for_owner",
    "list_universe_visits_for_participant",
    "mark_universe_invite_terminal",
    "mark_universe_visit_active",
    "mark_universe_visit_terminal",
]

_INVITE_STATUSES = {"active", "redeemed", "revoked", "expired"}
_VISIT_STATUSES = {
    "pending",
    "active",
    "expired",
    "rejected",
    "cancelled",
    "left",
    "revoked",
    "blocked",
}


@contextmanager
def _visit_write_tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用外层事务；独立 SQLite 写用 IMMEDIATE 串行读后写。"""
    if conn is not None:
        yield conn
        return
    with connect() as own:
        if not is_postgres():
            own.execute("BEGIN IMMEDIATE")
        yield own


def _required(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _lock_owner_world(
    tx: Connection, *, universe_id: str, owner_platform_user_id: str
) -> Dict[str, Any]:
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = tx.execute(
        "SELECT id, owner_platform_user_id, status, onboarding_state FROM universes "
        "WHERE id = ? AND owner_platform_user_id = ?" + suffix,
        (universe_id, owner_platform_user_id),
    ).fetchone()
    if row is None:
        raise ValueError("visit universe ownership mismatch")
    result = dict(row)
    if result["status"] != "active" or result["onboarding_state"] != "confirmed":
        raise ValueError("visit universe is not confirmed")
    return result


def ensure_universe_visit_slots(
    *,
    universe_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """在 owner world lock 内幂等补齐固定 1–3 号 visit slots。"""
    universe_id = _required(universe_id, "universe_id")
    owner_platform_user_id = _required(
        owner_platform_user_id, "owner_platform_user_id"
    )
    with _visit_write_tx(conn) as tx:
        _lock_owner_world(
            tx,
            universe_id=universe_id,
            owner_platform_user_id=owner_platform_user_id,
        )
        for slot_no in (1, 2, 3):
            tx.execute(
                "INSERT INTO universe_visit_slots(universe_id, slot_no) "
                "VALUES (?, ?) ON CONFLICT DO NOTHING",
                (universe_id, slot_no),
            )
        rows = tx.execute(
            "SELECT * FROM universe_visit_slots WHERE universe_id = ? "
            "ORDER BY slot_no ASC",
            (universe_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_universe_invite(
    *,
    universe_id: str,
    owner_platform_user_id: str,
    code_hash: str,
    code_prefix: str,
    expires_at: str,
    created_at: str,
    invite_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """在 owner world 的最小空 slot 中插入 active invite；明文 token 不入参。"""
    universe_id = _required(universe_id, "universe_id")
    owner_platform_user_id = _required(
        owner_platform_user_id, "owner_platform_user_id"
    )
    code_hash = _required(code_hash, "code_hash")
    code_prefix = _required(code_prefix, "code_prefix")
    expires_at = _required(expires_at, "expires_at")
    created_at = _required(created_at, "created_at")
    invite_id = invite_id or _new_id("winv")
    with _visit_write_tx(conn) as tx:
        ensure_universe_visit_slots(
            universe_id=universe_id,
            owner_platform_user_id=owner_platform_user_id,
            conn=tx,
        )
        slot = tx.execute(
            "SELECT slot_no FROM universe_visit_slots "
            "WHERE universe_id = ? AND occupant_id IS NULL "
            "ORDER BY slot_no ASC LIMIT 1",
            (universe_id,),
        ).fetchone()
        if slot is None:
            raise ValueError("world visit limit reached")
        tx.execute(
            """
            INSERT INTO universe_invites(
                id, universe_id, owner_platform_user_id, code_hash, code_prefix,
                status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                invite_id,
                universe_id,
                owner_platform_user_id,
                code_hash,
                code_prefix,
                expires_at,
                created_at,
                created_at,
            ),
        )
        updated = tx.execute(
            "UPDATE universe_visit_slots SET occupant_type = 'invite', occupant_id = ?, "
            "occupied_at = ? WHERE universe_id = ? AND slot_no = ? AND occupant_id IS NULL",
            (invite_id, created_at, universe_id, int(slot["slot_no"])),
        )
        if updated.rowcount != 1:
            raise RuntimeError("visit slot allocation lost")
        row = tx.execute(
            "SELECT * FROM universe_invites WHERE id = ?", (invite_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("universe invite was not created")
    return dict(row)


def get_universe_invite_for_owner(
    *,
    invite_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 invite id + owner 锚读取；跨 owner 返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM universe_invites WHERE id = ? AND owner_platform_user_id = ?",
            (invite_id, owner_platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_universe_invite_by_code_hash(
    *, code_hash: str, conn: Optional[Connection] = None, for_update: bool = False
) -> Optional[Dict[str, Any]]:
    """内部按完整 hash 解析 invite；公开 API 不得暴露该查询结果。"""
    suffix = " FOR UPDATE" if for_update and is_postgres() else ""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM universe_invites WHERE code_hash = ?" + suffix,
            (code_hash,),
        ).fetchone()
    return dict(row) if row else None


def list_universe_invites_for_owner(
    *,
    owner_platform_user_id: str,
    statuses: Optional[Sequence[str]] = None,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出 owner 自己的 invite，绝不返回其他世界行。"""
    safe_limit = max(1, min(int(limit), 100))
    params: list[Any] = [owner_platform_user_id]
    where = ["owner_platform_user_id = ?"]
    if statuses:
        cleaned = [status for status in statuses if status in _INVITE_STATUSES]
        if not cleaned:
            return []
        where.append("status IN (" + ",".join("?" for _ in cleaned) + ")")
        params.extend(cleaned)
    params.append(safe_limit)
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT * FROM universe_invites WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_pending_universe_visit(
    *,
    invite_id: str,
    universe_id: str,
    owner_platform_user_id: str,
    visitor_platform_user_id: str,
    pending_expires_at: str,
    created_at: str,
    visit_id: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """创建 pending visit，并把既有 invite slot 原位转为 visit occupant。"""
    if owner_platform_user_id == visitor_platform_user_id:
        raise ValueError("self invite is forbidden")
    visit_id = visit_id or _new_id("visit")
    with _visit_write_tx(conn) as tx:
        _lock_owner_world(
            tx,
            universe_id=universe_id,
            owner_platform_user_id=owner_platform_user_id,
        )
        suffix = " FOR UPDATE" if is_postgres() else ""
        invite = tx.execute(
            "SELECT * FROM universe_invites WHERE id = ? AND universe_id = ? "
            "AND owner_platform_user_id = ?" + suffix,
            (invite_id, universe_id, owner_platform_user_id),
        ).fetchone()
        if invite is None or invite["status"] != "active":
            raise ValueError("invite is not active")
        tx.execute(
            """
            INSERT INTO universe_visits(
                id, invite_id, universe_id, owner_platform_user_id,
                visitor_platform_user_id, status, pending_expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                visit_id,
                invite_id,
                universe_id,
                owner_platform_user_id,
                visitor_platform_user_id,
                pending_expires_at,
                created_at,
                created_at,
            ),
        )
        updated = tx.execute(
            "UPDATE universe_visit_slots SET occupant_type = 'visit', occupant_id = ?, "
            "occupied_at = ? WHERE universe_id = ? AND occupant_type = 'invite' "
            "AND occupant_id = ?",
            (visit_id, created_at, universe_id, invite_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("invite slot transfer failed")
        tx.execute(
            "UPDATE universe_invites SET status = 'redeemed', "
            "redeemed_by_platform_user_id = ?, redeemed_visit_id = ?, redeemed_at = ?, "
            "updated_at = ? WHERE id = ? AND status = 'active'",
            (
                visitor_platform_user_id,
                visit_id,
                created_at,
                created_at,
                invite_id,
            ),
        )
        row = tx.execute(
            "SELECT * FROM universe_visits WHERE id = ?", (visit_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("pending universe visit was not created")
    return dict(row)


def count_open_universe_visits_for_visitor(
    *, visitor_platform_user_id: str, conn: Optional[Connection] = None
) -> int:
    """按 B 真人锚聚合 pending+active visit 数。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT COUNT(*) AS n FROM universe_visits "
            "WHERE visitor_platform_user_id = ? AND status IN ('pending', 'active')",
            (visitor_platform_user_id,),
        ).fetchone()
    return int(row["n"] if row else 0)


def get_universe_visit_for_participant(
    *,
    visit_id: str,
    platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 visit id + participant 锚读取；旁观者返回 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM universe_visits WHERE id = ? "
            "AND (owner_platform_user_id = ? OR visitor_platform_user_id = ?)",
            (visit_id, platform_user_id, platform_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_universe_visit(
    *, visit_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """内部按 id 读取 visit；公开调用必须改用 participant-scoped 版本。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM universe_visits WHERE id = ?", (visit_id,)
        ).fetchone()
    return dict(row) if row else None


def list_universe_visits_for_participant(
    *,
    platform_user_id: str,
    statuses: Optional[Sequence[str]] = None,
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """列出当前真人作为 owner 或 visitor 参与的 visits。"""
    safe_limit = max(1, min(int(limit), 100))
    params: list[Any] = [platform_user_id, platform_user_id]
    where = ["(v.owner_platform_user_id = ? OR v.visitor_platform_user_id = ?)"]
    if statuses:
        cleaned = [status for status in statuses if status in _VISIT_STATUSES]
        if not cleaned:
            return []
        where.append("v.status IN (" + ",".join("?" for _ in cleaned) + ")")
        params.extend(cleaned)
    params.append(safe_limit)
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT v.*, owner.display_name AS owner_display_name, "
            "visitor.display_name AS visitor_display_name "
            "FROM universe_visits v "
            "JOIN platform_users owner ON owner.id = v.owner_platform_user_id "
            "JOIN platform_users visitor ON visitor.id = v.visitor_platform_user_id "
            "WHERE "
            + " AND ".join(where)
            + " ORDER BY v.created_at DESC, v.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def lock_platform_user_for_visit(
    *, platform_user_id: str, conn: Connection
) -> Dict[str, Any]:
    """按 M5 总锁序锁一个真人主体；不存在时拒绝继续。"""
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = conn.execute(
        "SELECT id, display_name FROM platform_users WHERE id = ?" + suffix,
        (platform_user_id,),
    ).fetchone()
    if row is None:
        raise ValueError("platform user not found")
    return dict(row)


def lock_universe_visit(
    *, visit_id: str, conn: Connection
) -> Optional[Dict[str, Any]]:
    """在调用方事务内锁 visit 行。"""
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = conn.execute(
        "SELECT * FROM universe_visits WHERE id = ?" + suffix, (visit_id,)
    ).fetchone()
    return dict(row) if row else None


def mark_universe_invite_terminal(
    *,
    invite_id: str,
    owner_platform_user_id: str,
    target_status: str,
    now: str,
    conn: Connection,
) -> Optional[Dict[str, Any]]:
    """把 active invite 置 revoked/expired 并释放其 world slot。"""
    if target_status not in {"revoked", "expired"}:
        raise ValueError("invalid invite terminal status")
    suffix = " FOR UPDATE" if is_postgres() else ""
    row = conn.execute(
        "SELECT * FROM universe_invites WHERE id = ? AND owner_platform_user_id = ?"
        + suffix,
        (invite_id, owner_platform_user_id),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == target_status:
        return dict(row)
    if row["status"] != "active":
        raise ValueError("invite is not active")
    if target_status == "revoked":
        conn.execute(
            "UPDATE universe_invites SET status = 'revoked', revoked_at = ?, "
            "updated_at = ? WHERE id = ? AND status = 'active'",
            (now, now, invite_id),
        )
    else:
        conn.execute(
            "UPDATE universe_invites SET status = 'expired', updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (now, invite_id),
        )
    conn.execute(
        "UPDATE universe_visit_slots SET occupant_type = NULL, occupant_id = NULL, "
        "occupied_at = NULL WHERE universe_id = ? AND occupant_type = 'invite' "
        "AND occupant_id = ?",
        (row["universe_id"], invite_id),
    )
    updated = conn.execute(
        "SELECT * FROM universe_invites WHERE id = ?", (invite_id,)
    ).fetchone()
    return dict(updated) if updated else None


def mark_universe_visit_active(
    *, visit_id: str, accepted_at: str, expires_at: str, conn: Connection
) -> Dict[str, Any]:
    """把已锁定 pending visit 单向转 active；slot occupant 保持不变。"""
    updated = conn.execute(
        "UPDATE universe_visits SET status = 'active', accepted_at = ?, expires_at = ?, "
        "updated_at = ? WHERE id = ? AND status = 'pending'",
        (accepted_at, expires_at, accepted_at, visit_id),
    )
    if updated.rowcount != 1:
        raise ValueError("visit is not pending")
    row = conn.execute(
        "SELECT * FROM universe_visits WHERE id = ?", (visit_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("visit disappeared after activation")
    return dict(row)


def mark_universe_visit_terminal(
    *,
    visit_id: str,
    expected_status: str,
    target_status: str,
    now: str,
    terminal_reason: str,
    conn: Connection,
) -> Dict[str, Any]:
    """终结 pending/active visit、释放 slot，并把既有真人会话单向只读。"""
    if expected_status not in {"pending", "active"}:
        raise ValueError("invalid expected visit status")
    if target_status not in _VISIT_STATUSES - {"pending", "active"}:
        raise ValueError("invalid visit terminal status")
    updated = conn.execute(
        "UPDATE universe_visits SET status = ?, terminal_at = ?, terminal_reason = ?, "
        "updated_at = ? WHERE id = ? AND status = ?",
        (target_status, now, terminal_reason, now, visit_id, expected_status),
    )
    if updated.rowcount != 1:
        raise ValueError(f"visit is not {expected_status}")
    row = conn.execute(
        "SELECT * FROM universe_visits WHERE id = ?", (visit_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("visit disappeared after terminal transition")
    conn.execute(
        "UPDATE universe_visit_slots SET occupant_type = NULL, occupant_id = NULL, "
        "occupied_at = NULL WHERE universe_id = ? AND occupant_type = 'visit' "
        "AND occupant_id = ?",
        (row["universe_id"], visit_id),
    )
    conn.execute(
        "UPDATE human_conversations SET status = 'read_only', updated_at = ? "
        "WHERE visit_id = ? AND status = 'active'",
        (now, visit_id),
    )
    return dict(row)
