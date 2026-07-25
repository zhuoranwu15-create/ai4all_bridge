"""接入节点登记与账号路由的共享持久化原语。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.db._core import _clean_text, connect

__all__ = [
    "get_access_node",
    "pick_node",
    "resolve_node_for_account",
    "set_account_assigned_node",
    "should_inline_dispatch_for_account",
    "upsert_access_node",
]


def set_account_assigned_node(*, account_id: str, node_id: Optional[str]) -> None:
    """写账号到接入节点的路由归属；空 node_id 表示清除归属。"""

    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET assigned_node_id = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (_clean_text(node_id), cleaned_account_id),
        )


def resolve_node_for_account(account_id: str) -> Optional[str]:
    """反查账号归属节点；无归属或无账号时返回 None。"""

    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT assigned_node_id FROM accounts WHERE id = ?",
            (cleaned_account_id,),
        ).fetchone()
    if row is None:
        return None
    return row["assigned_node_id"] or None


def should_inline_dispatch_for_account(
    account_id: Optional[str], settings_obj: Any
) -> bool:
    """判断出站是否可由当前进程按账号归属即时发送。"""

    roles = {
        role.strip().lower()
        for role in (getattr(settings_obj, "ai4all_role", "") or "").split(",")
        if role.strip()
    }
    if "standalone" in roles:
        return True
    if not getattr(settings_obj, "local_node_inline_dispatch", False):
        return False
    account_node = resolve_node_for_account(account_id or "") or (
        getattr(settings_obj, "default_node_id", "") or None
    )
    return bool(account_node) and account_node == (
        getattr(settings_obj, "node_id", "") or None
    )


def upsert_access_node(
    *,
    node_id: str,
    base_url: Optional[str] = None,
    egress_ip: Optional[str] = None,
    session_count: Optional[int] = None,
    max_sessions: Optional[int] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """登记或更新接入节点，只覆盖显式传入的字段并刷新心跳。"""

    cleaned_node_id = _clean_text(node_id)
    if not cleaned_node_id:
        return None
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO access_nodes(node_id, session_count, status, last_heartbeat_at)
            VALUES (?, 0, 'online', strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (cleaned_node_id,),
        )
        conn.execute(
            """
            UPDATE access_nodes
            SET base_url = COALESCE(?, base_url),
                egress_ip = COALESCE(?, egress_ip),
                session_count = COALESCE(?, session_count),
                max_sessions = COALESCE(?, max_sessions),
                status = COALESCE(?, status),
                last_heartbeat_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE node_id = ?
            """,
            (
                _clean_text(base_url),
                _clean_text(egress_ip),
                int(session_count) if session_count is not None else None,
                int(max_sessions) if max_sessions is not None else None,
                _clean_text(status),
                cleaned_node_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM access_nodes WHERE node_id = ?", (cleaned_node_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_access_node(*, node_id: str) -> Optional[Dict[str, Any]]:
    """读取单个接入节点登记。"""

    cleaned_node_id = _clean_text(node_id)
    if not cleaned_node_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM access_nodes WHERE node_id = ?", (cleaned_node_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def pick_node(*, preferred_node_id: Optional[str] = None) -> Optional[str]:
    """优先选择指定在线节点，否则选择会话数最少的可用节点。"""

    cleaned_pref = _clean_text(preferred_node_id)
    with connect() as conn:
        if cleaned_pref:
            row = conn.execute(
                "SELECT node_id FROM access_nodes WHERE node_id = ? AND status = 'online'",
                (cleaned_pref,),
            ).fetchone()
            if row is not None:
                return row["node_id"]
        row = conn.execute(
            """
            SELECT node_id FROM access_nodes
            WHERE status = 'online'
              AND (max_sessions IS NULL OR max_sessions <= 0 OR session_count < max_sessions)
            ORDER BY session_count ASC, node_id ASC
            LIMIT 1
            """
        ).fetchone()
    return row["node_id"] if row is not None else None
