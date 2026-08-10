#!/usr/bin/env python3
"""只读检查鸣蝉能否在保留朝夕 legacy World 的前提下首次启用。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cleanup_legacy_app_test_data import (  # noqa: E402
    build_cleanup_plan,
    configure_database_url_override,
)
from app.bootstrap.product_registry import MINGCHAN_APP_ID  # noqa: E402
from app.db import connect  # noqa: E402


def _has_product_owner_unique(conn) -> bool:
    """确认数据库已具备 ``universes(app_id, owner)`` 唯一契约。"""
    row = conn.execute(
        """
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'universes'
          AND indexdef LIKE 'CREATE UNIQUE INDEX% (app_id, owner_platform_user_id)%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def build_preservation_plan(*, conn=None) -> Dict[str, Any]:
    """构造“保留朝夕、鸣蝉空产品启动”的只读验收报告。"""

    if conn is None:
        with connect() as tx:
            return build_preservation_plan(conn=tx)
    legacy = build_cleanup_plan(conn=conn)
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    schema_version = int(version_row["version"] if version_row else 0)

    def count(sql: str) -> int:
        row = conn.execute(sql, (MINGCHAN_APP_ID,)).fetchone()
        return int(row["n"] if row else 0)

    mingchan_counts = {
        "worlds": count("SELECT COUNT(*) AS n FROM universes WHERE app_id=?"),
        "templates": count(
            "SELECT COUNT(*) AS n FROM character_templates WHERE app_id=?"
        ),
        "notifications": count(
            "SELECT COUNT(*) AS n FROM app_notifications WHERE app_id=?"
        ),
        "memberships": count(
            "SELECT COUNT(*) AS n FROM product_memberships WHERE app_id=?"
        ),
        "sessions": count(
            "SELECT COUNT(*) AS n FROM platform_user_sessions WHERE app_id=?"
        ),
        "accounts": count("SELECT COUNT(*) AS n FROM accounts WHERE app_id=?"),
    }
    product_owner_unique = _has_product_owner_unique(conn)
    empty_mingchan = not any(mingchan_counts.values())
    return {
        "mode": "preserve_legacy_zhaoxi",
        "schema_version": schema_version,
        "product_owner_unique": product_owner_unique,
        "legacy_counts_retained": legacy["counts"],
        "mingchan_counts": mingchan_counts,
        "safe_to_enable": schema_version >= 63
        and product_owner_unique
        and empty_mingchan,
        "retention": {
            "zhaoxi_worlds_and_children": "always",
            "weixin_bindings_messages_memory_billing": "always",
            "platform_users": "shared_identity_only",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="鸣蝉 clean-start 只读预检")
    parser.add_argument(
        "--database-url",
        default=None,
        help="可选覆盖 PostgreSQL DATABASE_URL",
    )
    parser.add_argument(
        "--cleanup-legacy",
        action="store_true",
        help="仅查看旧的删除型 cleanup plan；默认采用保留朝夕方案",
    )
    args = parser.parse_args()
    try:
        configure_database_url_override(args.database_url)
    except ValueError as err:
        parser.error(str(err))
    report = build_cleanup_plan() if args.cleanup_legacy else build_preservation_plan()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    key = "safe_to_apply" if args.cleanup_legacy else "safe_to_enable"
    return 0 if report[key] else 2


if __name__ == "__main__":
    raise SystemExit(main())
