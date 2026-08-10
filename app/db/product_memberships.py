"""产品 membership 存储原语。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.bootstrap.product_registry import (
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db._backend import Connection, Row
from app.db._core import _tx, connect

__all__ = [
    "ensure_product_membership",
    "get_product_membership",
    "list_product_memberships",
    "require_active_product_membership",
    "update_product_membership_status",
]

_MEMBERSHIP_STATUSES = {"active", "disabled"}


def _decode_membership(row: Row) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["settings"] = json.loads(item.pop("settings_json") or "{}")
    except json.JSONDecodeError:
        item["settings"] = {}
        item["settings_decode_error"] = True
    return item


def _registered_app_id(app_id: str, registry: ProductRegistry) -> str:
    return registry.require_enabled(app_id).app_id


def _get_product_membership_in_conn(
    conn: Connection, *, platform_user_id: str, app_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT platform_user_id, app_id, status, daily_limit, rpm_limit,
               settings_json, created_at, updated_at
        FROM product_memberships
        WHERE platform_user_id=? AND app_id=?
        """,
        (platform_user_id, app_id),
    ).fetchone()
    return _decode_membership(row) if row else None


def _ensure_product_membership_in_conn(
    conn: Connection,
    *,
    platform_user_id: str,
    app_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    registered_app_id = _registered_app_id(app_id, registry)
    user = conn.execute(
        "SELECT id FROM platform_users WHERE id=?", (platform_user_id,)
    ).fetchone()
    if user is None:
        raise ValueError("platform_user not found")
    cursor = conn.execute(
        """
        INSERT INTO product_memberships(platform_user_id, app_id, status, updated_at)
        VALUES (?, ?, 'active', to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
        ON CONFLICT(platform_user_id, app_id) DO NOTHING
        """,
        (platform_user_id, registered_app_id),
    )
    membership = _get_product_membership_in_conn(
        conn, platform_user_id=platform_user_id, app_id=registered_app_id
    )
    if membership is None:
        raise ValueError("platform_user not found")
    return {
        "membership": membership,
        "is_new_membership": cursor.rowcount == 1,
    }


def ensure_product_membership(
    *,
    platform_user_id: str,
    app_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """幂等创建 membership；既有 disabled 行不会被隐式重新启用。"""

    with _tx(conn) as tx:
        return _ensure_product_membership_in_conn(
            tx,
            platform_user_id=platform_user_id,
            app_id=app_id,
            registry=registry,
        )


def get_product_membership(
    *,
    platform_user_id: str,
    app_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Optional[Dict[str, Any]]:
    """读取一个已注册产品的 membership；无行即未加入。"""

    registered_app_id = _registered_app_id(app_id, registry)
    with connect() as conn:
        return _get_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=registered_app_id,
        )


def list_product_memberships(*, platform_user_id: str) -> List[Dict[str, Any]]:
    """列出真人已有 membership，不据此扩大服务端注册表。"""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT platform_user_id, app_id, status, daily_limit, rpm_limit,
                   settings_json, created_at, updated_at
            FROM product_memberships
            WHERE platform_user_id=?
            ORDER BY app_id
            """,
            (platform_user_id,),
        ).fetchall()
    return [_decode_membership(row) for row in rows]


def _require_active_product_membership_in_conn(
    conn: Connection,
    *,
    platform_user_id: str,
    app_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    registered_app_id = _registered_app_id(app_id, registry)
    membership = _get_product_membership_in_conn(
        conn,
        platform_user_id=platform_user_id,
        app_id=registered_app_id,
    )
    if membership is None or membership["status"] != "active":
        raise ValueError("active product membership required")
    return membership


def require_active_product_membership(
    *,
    platform_user_id: str,
    app_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """要求 membership 存在且 active，否则 fail closed。"""

    with _tx(conn) as tx:
        return _require_active_product_membership_in_conn(
            tx,
            platform_user_id=platform_user_id,
            app_id=app_id,
            registry=registry,
        )


def update_product_membership_status(
    *,
    platform_user_id: str,
    app_id: str,
    status: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Optional[Dict[str, Any]]:
    """显式启用/停用既有 membership；缺失时不创建占位行。"""

    cleaned_status = str(status or "").strip()
    if cleaned_status not in _MEMBERSHIP_STATUSES:
        raise ValueError("membership status must be active or disabled")
    registered_app_id = _registered_app_id(app_id, registry)
    with connect() as conn:
        conn.execute(
            """
            UPDATE product_memberships
            SET status=?, updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id=? AND app_id=?
            """,
            (cleaned_status, platform_user_id, registered_app_id),
        )
        return _get_product_membership_in_conn(
            conn,
            platform_user_id=platform_user_id,
            app_id=registered_app_id,
        )
