"""审核任务产品归属：迁移回填、写入校验与查询隔离。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db._backend import IntegrityError


def _insert_account(*, account_id: str, app_id: str) -> None:
    from app.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, channel, app_id) VALUES (?, 'native', ?)",
            (account_id, app_id),
        )


def _create_task(*, account_id: str, app_id: str, suffix: str) -> dict:
    from app.db import create_content_moderation_task

    return create_content_moderation_task(
        app_id=app_id,
        account_id=account_id,
        session_id=None,
        source_type="message",
        source_id=f"source-{suffix}",
        message_db_id=None,
        outbound_message_id=None,
        direction="inbound",
        content_kind="text",
        status="needs_review",
        risk_level="review",
        snapshot_text=f"text-{suffix}",
        policy_version="test_policy",
        idempotency_key=f"moderation-scope:{suffix}",
    )


def test_moderation_persistence_requires_matching_product_scope(fresh_db):
    from app.db import get_content_moderation_stats, list_content_moderation_tasks

    _insert_account(account_id="acc-scope-zx", app_id="zhaoxi")
    _insert_account(account_id="acc-scope-mc", app_id="mingchan")
    zhaoxi_task = _create_task(
        account_id="acc-scope-zx",
        app_id="zhaoxi",
        suffix="zx",
    )
    mingchan_task = _create_task(
        account_id="acc-scope-mc",
        app_id="mingchan",
        suffix="mc",
    )

    assert zhaoxi_task["app_id"] == "zhaoxi"
    assert mingchan_task["app_id"] == "mingchan"
    with pytest.raises(ValueError, match="does not belong"):
        _create_task(
            account_id="acc-scope-mc",
            app_id="zhaoxi",
            suffix="scope-drift",
        )

    mingchan_tasks = list_content_moderation_tasks(app_id="mingchan")
    assert [task["id"] for task in mingchan_tasks] == [mingchan_task["id"]]
    assert get_content_moderation_stats(app_id="zhaoxi")["total"] == 1
    assert get_content_moderation_stats(app_id="mingchan")["total"] == 1


def test_m0070_backfills_existing_tasks_and_enforces_required_app_id(
    test_settings, empty_pg_database
):
    from app.db import close_pg_pool, connect, migrate_db_through

    with patch("app.db.settings", test_settings):
        try:
            migrate_db_through(target_version=69, expected_current_version=0)
            with connect() as conn:
                conn.execute(
                    "INSERT INTO accounts(id, channel, app_id) "
                    "VALUES ('legacy-zx', 'openclaw-weixin', 'zhaoxi')"
                )
                conn.execute(
                    "INSERT INTO accounts(id, channel, app_id) "
                    "VALUES ('legacy-mc', 'native', 'mingchan')"
                )
                for task_id, account_id in (
                    ("legacy-task-zx", "legacy-zx"),
                    ("legacy-task-mc", "legacy-mc"),
                ):
                    conn.execute(
                        """
                        INSERT INTO content_moderation_tasks(
                            id, account_id, source_type, source_id, direction,
                            content_kind, status, risk_level, policy_version,
                            idempotency_key
                        )
                        VALUES (?, ?, 'message', ?, 'inbound', 'text',
                                'queued', 'unknown', 'legacy_policy', ?)
                        """,
                        (task_id, account_id, task_id, f"legacy:{task_id}"),
                    )

            assert migrate_db_through(
                target_version=70,
                expected_current_version=69,
            ) == {"before": 69, "after": 70}

            with connect() as conn:
                rows = conn.execute(
                    "SELECT id, app_id FROM content_moderation_tasks ORDER BY id"
                ).fetchall()
                assert [(row["id"], row["app_id"]) for row in rows] == [
                    ("legacy-task-mc", "mingchan"),
                    ("legacy-task-zx", "zhaoxi"),
                ]
                column = conn.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema=current_schema() "
                    "AND table_name='content_moderation_tasks' "
                    "AND column_name='app_id'"
                ).fetchone()
                index_rows = conn.execute(
                    "SELECT indexname AS name FROM pg_indexes "
                    "WHERE schemaname=current_schema()"
                ).fetchall()
                assert column["is_nullable"] == "NO"
                index_names = {row["name"] for row in index_rows}
                assert {
                    "ix_moderation_tasks_app_queue",
                    "ix_moderation_tasks_app_account_created",
                }.issubset(index_names)

            with pytest.raises(IntegrityError):
                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO content_moderation_tasks(
                            id, account_id, source_type, source_id, direction,
                            content_kind, status, risk_level, policy_version,
                            idempotency_key
                        )
                        VALUES ('missing-app', 'legacy-zx', 'message', 'missing-app',
                                'inbound', 'text', 'queued', 'unknown',
                                'legacy_policy', 'legacy:missing-app')
                        """
                    )
        finally:
            close_pg_pool()
