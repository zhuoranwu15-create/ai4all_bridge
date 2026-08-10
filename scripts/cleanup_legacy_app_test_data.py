#!/usr/bin/env python3
"""受控清理拆分前 ``zhaoxi`` 名下的 App/Companion World 测试域。

默认只输出 plan；只有显式 ``--apply`` 才写库。脚本永不删除 ``platform_users``、
product membership、账务、媒体或 runtime account。发现真实微信绑定，或 resident account
存在非 App session 消息时整批拒绝写入，避免把共用真人的朝夕资产当测试数据清理。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bootstrap.product_registry import MINGCHAN_APP_ID, ZHAOXI_APP_ID  # noqa: E402
from app.db import connect  # noqa: E402


def configure_database_url_override(database_url: Optional[str]) -> None:
    """为 cleanup CLI 设置可选的 PostgreSQL URL。"""

    if database_url is None:
        return
    value = database_url.strip()
    if not value.lower().startswith(("postgres://", "postgresql://")):
        raise ValueError("--database-url only accepts PostgreSQL URLs")
    from app.config import settings

    settings.database_url = value


def _scalar(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(next(iter(dict(row).values())) if row is not None else 0)


def _legacy_world_where(alias: str = "u") -> str:
    return f"{alias}.app_id = ?"


def build_cleanup_plan(*, conn=None) -> Dict[str, Any]:
    """构造不含手机号、消息正文和内部 ID 的只读清理计划。"""

    if conn is None:
        with connect() as tx:
            return build_cleanup_plan(conn=tx)

    legacy_worlds = _scalar(
        conn, "SELECT COUNT(*) FROM universes u WHERE " + _legacy_world_where(),
        (ZHAOXI_APP_ID,),
    )
    legacy_residents = _scalar(
        conn,
        "SELECT COUNT(*) FROM universe_residents r JOIN universes u ON u.id=r.universe_id "
        "WHERE " + _legacy_world_where(),
        (ZHAOXI_APP_ID,),
    )
    resident_accounts = _scalar(
        conn,
        "SELECT COUNT(DISTINCT r.runtime_account_id) FROM universe_residents r "
        "JOIN universes u ON u.id=r.universe_id WHERE " + _legacy_world_where()
        + " AND r.runtime_account_id IS NOT NULL",
        (ZHAOXI_APP_ID,),
    )
    weixin_bindings = _scalar(
        conn,
        "SELECT COUNT(*) FROM channel_bindings b WHERE b.channel='openclaw-weixin' "
        "AND b.account_id IN (SELECT r.runtime_account_id FROM universe_residents r "
        "JOIN universes u ON u.id=r.universe_id WHERE " + _legacy_world_where()
        + " AND r.runtime_account_id IS NOT NULL)",
        (ZHAOXI_APP_ID,),
    )
    non_app_messages = _scalar(
        conn,
        "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE m.account_id IN (SELECT r.runtime_account_id FROM universe_residents r "
        "JOIN universes u ON u.id=r.universe_id WHERE " + _legacy_world_where()
        + " AND r.runtime_account_id IS NOT NULL) "
        "AND s.session_key <> '__app_active__' "
        "AND s.session_key NOT LIKE '__app_active__:%'",
        (ZHAOXI_APP_ID,),
    )
    mingchan_worlds = _scalar(
        conn, "SELECT COUNT(*) FROM universes WHERE app_id=?", (MINGCHAN_APP_ID,)
    )
    counts = {
        "legacy_worlds": legacy_worlds,
        "legacy_residents": legacy_residents,
        "legacy_resident_runtime_accounts_retained": resident_accounts,
        "legacy_character_templates": _scalar(
            conn,
            "SELECT COUNT(*) FROM character_templates WHERE app_id=?",
            (ZHAOXI_APP_ID,),
        ),
        "legacy_app_notifications": _scalar(
            conn,
            "SELECT COUNT(*) FROM app_notifications WHERE app_id=?",
            (ZHAOXI_APP_ID,),
        ),
        "mingchan_worlds_retained": mingchan_worlds,
        "protected_weixin_bindings": weixin_bindings,
        "protected_non_app_messages": non_app_messages,
    }
    blockers = {
        "resident_account_has_weixin_binding": weixin_bindings > 0,
        "resident_account_has_non_app_messages": non_app_messages > 0,
    }
    return {
        "mode": "plan",
        "target_app_id": ZHAOXI_APP_ID,
        "counts": counts,
        "blockers": blockers,
        "safe_to_apply": not any(blockers.values()),
        "retention": {
            "platform_users": "always",
            "product_memberships_and_billing": "always",
            "runtime_accounts_and_account_data": "always",
            "unscoped_resident_drafts": "always; table has no app_id/world anchor",
            "media_assets": "always; reconcile manually after domain cleanup",
            "mingchan_rows": "always",
        },
    }


def _delete(conn, sql: str, params=()) -> int:
    cursor = conn.execute(sql, params)
    return max(int(cursor.rowcount or 0), 0)


def apply_cleanup(*, conn=None) -> Dict[str, Any]:
    """在一个事务内删除旧 World 产品域；有保护项时不执行任何写入。"""

    if conn is None:
        with connect() as tx:
            return apply_cleanup(conn=tx)
    before = build_cleanup_plan(conn=conn)
    if not before["safe_to_apply"]:
        raise RuntimeError("legacy App cleanup blocked by protected Weixin/account data")

    app = (ZHAOXI_APP_ID,)
    world_ids = "SELECT id FROM universes WHERE app_id = ?"
    resident_ids = (
        "SELECT r.id FROM universe_residents r JOIN universes u ON u.id=r.universe_id "
        "WHERE u.app_id = ?"
    )
    post_ids = "SELECT id FROM universe_posts WHERE universe_id IN (" + world_ids + ")"
    visit_ids = "SELECT id FROM universe_visits WHERE universe_id IN (" + world_ids + ")"
    conversation_ids = "SELECT id FROM human_conversations WHERE visit_id IN (" + visit_ids + ")"
    event_ids = (
        "SELECT id FROM resident_lifecycle_events WHERE universe_id IN (" + world_ids + ")"
    )
    wish_ids = "SELECT id FROM resident_wishes WHERE universe_id IN (" + world_ids + ")"

    deleted: Dict[str, int] = {}
    deleted["human_chat_reports"] = _delete(
        conn, "DELETE FROM human_chat_reports WHERE conversation_id IN (" + conversation_ids + ")", app
    )
    deleted["human_messages"] = _delete(
        conn, "DELETE FROM human_messages WHERE conversation_id IN (" + conversation_ids + ")", app
    )
    deleted["human_conversations"] = _delete(
        conn, "DELETE FROM human_conversations WHERE id IN (" + conversation_ids + ")", app
    )
    deleted["universe_visits"] = _delete(
        conn, "DELETE FROM universe_visits WHERE id IN (" + visit_ids + ")", app
    )
    deleted["universe_invites"] = _delete(
        conn, "DELETE FROM universe_invites WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["universe_visit_slots"] = _delete(
        conn, "DELETE FROM universe_visit_slots WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["resident_wish_jobs"] = _delete(
        conn, "DELETE FROM resident_wish_jobs WHERE wish_id IN (" + wish_ids + ")", app
    )
    # 两表互相保存引用；必须先同时断开，再按逆序删除。
    _delete(conn, "UPDATE resident_wishes SET letter_id=NULL WHERE id IN (" + wish_ids + ")", app)
    _delete(
        conn,
        "UPDATE character_letters SET wish_id=NULL WHERE universe_id IN (" + world_ids + ")",
        app,
    )
    deleted["resident_wishes"] = _delete(
        conn, "DELETE FROM resident_wishes WHERE id IN (" + wish_ids + ")", app
    )
    deleted["resident_lifecycle_event_actions"] = _delete(
        conn, "DELETE FROM resident_lifecycle_event_actions WHERE event_id IN (" + event_ids + ")", app
    )
    _delete(
        conn,
        "UPDATE resident_lifecycle_events SET farewell_post_id=NULL WHERE id IN (" + event_ids + ")",
        app,
    )
    deleted["resident_lifecycle_events"] = _delete(
        conn, "DELETE FROM resident_lifecycle_events WHERE id IN (" + event_ids + ")", app
    )
    deleted["app_notifications"] = _delete(
        conn, "DELETE FROM app_notifications WHERE app_id=?", app
    )
    deleted["companion_world_outbox"] = _delete(
        conn, "DELETE FROM companion_world_outbox WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["universe_post_media"] = _delete(
        conn, "DELETE FROM universe_post_media WHERE post_id IN (" + post_ids + ")", app
    )
    deleted["universe_posts"] = _delete(
        conn, "DELETE FROM universe_posts WHERE id IN (" + post_ids + ")", app
    )
    deleted["character_letters"] = _delete(
        conn, "DELETE FROM character_letters WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["universe_memory_facts"] = _delete(
        conn, "DELETE FROM universe_memory_facts WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["ai_conversations"] = _delete(
        conn, "DELETE FROM ai_conversations WHERE universe_id IN (" + world_ids + ")", app
    )
    deleted["universe_residents"] = _delete(
        conn, "DELETE FROM universe_residents WHERE id IN (" + resident_ids + ")", app
    )
    deleted["universes"] = _delete(conn, "DELETE FROM universes WHERE app_id=?", app)
    deleted["character_letter_catalog"] = _delete(
        conn,
        "DELETE FROM character_letter_catalog WHERE character_template_id IN "
        "(SELECT id FROM character_templates WHERE app_id=?)",
        app,
    )
    deleted["character_templates"] = _delete(
        conn, "DELETE FROM character_templates WHERE app_id=?", app
    )

    after = build_cleanup_plan(conn=conn)
    if after["counts"]["legacy_worlds"] or after["counts"]["legacy_residents"]:
        raise RuntimeError("legacy App cleanup reconcile failed")
    return {
        "mode": "apply",
        "target_app_id": ZHAOXI_APP_ID,
        "deleted": deleted,
        "reconcile": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="清理拆分前 legacy App/World 测试域")
    parser.add_argument("--apply", action="store_true", help="显式执行；默认仅输出 plan")
    parser.add_argument(
        "--database-url",
        default=None,
        help="可选覆盖 PostgreSQL DATABASE_URL",
    )
    args = parser.parse_args()
    try:
        configure_database_url_override(args.database_url)
    except ValueError as err:
        parser.error(str(err))
    report = apply_cleanup() if args.apply else build_cleanup_plan()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("safe_to_apply", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
