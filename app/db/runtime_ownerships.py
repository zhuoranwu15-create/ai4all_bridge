"""跨产品 runtime account 真人归属投影。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.bootstrap.product_registry import PRODUCTION_PRODUCT_REGISTRY, ProductRegistry
from app.db._backend import Connection
from app.db._core import _tx
from app.db.product_memberships import _require_active_product_membership_in_conn

__all__ = ["ensure_runtime_ownership", "get_runtime_ownership"]


def ensure_runtime_ownership(
    *,
    runtime_account_id: str,
    platform_user_id: str,
    app_id: str,
    source_type: str,
    source_id: str,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """幂等建立 runtime account 归属，并严格校验账号与 membership 的产品一致性。"""

    registered_app_id = registry.require_enabled(app_id).app_id
    with _tx(conn) as tx:
        _require_active_product_membership_in_conn(
            tx,
            platform_user_id=platform_user_id,
            app_id=registered_app_id,
            registry=registry,
        )
        account = tx.execute(
            "SELECT app_id FROM accounts WHERE id=?", (runtime_account_id,)
        ).fetchone()
        if account is None:
            raise ValueError("runtime account not found")
        if str(account["app_id"]) != registered_app_id:
            raise ValueError("runtime ownership app mismatch")
        tx.execute(
            """
            INSERT INTO runtime_ownerships(
                runtime_account_id, platform_user_id, app_id,
                owner_kind, source_type, source_id, status, updated_at
            )
            VALUES (?, ?, ?, 'user', ?, ?, 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
            ON CONFLICT(runtime_account_id) DO NOTHING
            """,
            (
                runtime_account_id,
                platform_user_id,
                registered_app_id,
                source_type,
                source_id,
            ),
        )
        row = tx.execute(
            "SELECT * FROM runtime_ownerships WHERE runtime_account_id=?",
            (runtime_account_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("runtime ownership was not created")
        if (
            str(row["platform_user_id"]) != platform_user_id
            or str(row["app_id"]) != registered_app_id
            or str(row["source_type"]) != source_type
            or str(row["source_id"]) != source_id
            or str(row["status"]) != "active"
        ):
            raise ValueError("runtime ownership conflict")
        return dict(row)


def get_runtime_ownership(
    *, runtime_account_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """读取一个 active runtime account 的归属。"""

    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM runtime_ownerships
            WHERE runtime_account_id=? AND status='active'
            """,
            (runtime_account_id,),
        ).fetchone()
    return dict(row) if row else None
