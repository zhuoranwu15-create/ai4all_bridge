"""朝夕产品级账号清除；只处理朝夕 membership、session、账号与角色模板。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.db._core import connect
from app.db.lifecycle import unbind_and_wipe_account
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    soft_delete_all_creator_role_templates,
)


def list_runtime_accounts_for_deletion(*, platform_user_id: str) -> List[str]:
    """列出真人通过 active owner binding 持有的朝夕账号。"""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT a.id
            FROM accounts a
            JOIN account_owner_bindings b ON b.account_id = a.id
            WHERE b.platform_user_id = ? AND b.status = 'active'
              AND b.app_id = ? AND a.app_id = ?
            ORDER BY a.id
            """,
            (platform_user_id, ZHAOXI_APP_ID, ZHAOXI_APP_ID),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def execute_account_deletion(
    *, platform_user_id: str, app_id: str, now: datetime
) -> Dict[str, Any]:
    """幂等清除朝夕产品资产，拒绝任何动态产品选择。"""

    if app_id != ZHAOXI_APP_ID:
        raise ValueError("zhaoxi account deletion requires app_id=zhaoxi")
    stats: Dict[str, Any] = {
        "accounts_wiped": 0,
        "messages_deleted": 0,
        "sessions_deleted": 0,
        "memory_events_deleted": 0,
        "dreaming_memory_items_deleted": 0,
        "profile_files_deleted": 0,
    }
    for account_id in list_runtime_accounts_for_deletion(
        platform_user_id=platform_user_id
    ):
        wiped = unbind_and_wipe_account(account_id=account_id)
        stats["accounts_wiped"] += 1
        for key in tuple(stats)[1:]:
            stats[key] += int(wiped.get(key) or 0)

    current = now.strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        stats["creator_role_templates_deleted"] = int(
            soft_delete_all_creator_role_templates(
                creator_platform_user_id=platform_user_id,
                app_id=ZHAOXI_APP_ID,
                changed_at=current,
                conn=conn,
            )
            or 0
        )
        stats["sessions_revoked"] = int(
            conn.execute(
                "DELETE FROM platform_user_sessions "
                "WHERE platform_user_id = ? AND app_id = ?",
                (platform_user_id, ZHAOXI_APP_ID),
            ).rowcount
            or 0
        )
        stats["memberships_deleted"] = int(
            conn.execute(
                "DELETE FROM product_memberships "
                "WHERE platform_user_id = ? AND app_id = ?",
                (platform_user_id, ZHAOXI_APP_ID),
            ).rowcount
            or 0
        )
    return stats


__all__ = ["execute_account_deletion", "list_runtime_accounts_for_deletion"]
