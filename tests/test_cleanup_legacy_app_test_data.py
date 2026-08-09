"""朝夕 legacy App/World cleanup 的只读、隔离、幂等与保护门禁。"""
from __future__ import annotations

from typing import Optional

import pytest

import app.db as db
from scripts.cleanup_legacy_app_test_data import (
    apply_cleanup,
    build_cleanup_plan,
    configure_database_url_override,
)
from scripts.precheck_mingchan_clean_start import build_preservation_plan


def test_cleanup_database_url_override_rejects_sqlite_url():
    """SQLite URL 不能静默回落标准库；临时 SQLite 必须显式设置路径。"""

    with pytest.raises(ValueError, match="only accepts PostgreSQL"):
        configure_database_url_override("sqlite:////private/tmp/cleanup.sqlite3")


def _seed_world(
    *, app_id: str, suffix: str, runtime_account_id: Optional[str] = None
) -> None:
    """直接写入拆分前态；产品 persistence 已有意拒绝创建朝夕 World。"""

    user = db.create_or_get_platform_user_by_phone(
        phone=f"1997700{suffix.zfill(4)}",
        display_name=f"cleanup-{suffix}",
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO universes(id, owner_platform_user_id, app_id)
            VALUES (?, ?, ?)
            """,
            (f"world_{suffix}", user["id"], app_id),
        )
        conn.execute(
            """
            INSERT INTO character_templates(id, app_id, source_type, name)
            VALUES (?, ?, 'operations', ?)
            """,
            (f"template_{suffix}", app_id, f"template-{suffix}"),
        )
        if runtime_account_id is not None:
            conn.execute(
                """
                INSERT INTO universe_residents(
                    id, universe_id, character_template_id, template_version,
                    runtime_account_id, origin, status
                ) VALUES (?, ?, ?, 'v1', ?, 'legacy', 'active')
                """,
                (
                    f"resident_{suffix}",
                    f"world_{suffix}",
                    f"template_{suffix}",
                    runtime_account_id,
                ),
            )


def test_cleanup_plan_apply_and_idempotency_preserve_mingchan(fresh_db):
    _seed_world(app_id="zhaoxi", suffix="1")
    _seed_world(app_id="mingchan", suffix="2")

    before = build_cleanup_plan()
    assert before["mode"] == "plan"
    assert before["safe_to_apply"] is True
    assert before["counts"]["legacy_worlds"] == 1
    assert before["counts"]["mingchan_worlds_retained"] == 1
    # plan 必须完全只读。
    assert build_cleanup_plan()["counts"] == before["counts"]

    applied = apply_cleanup()
    assert applied["deleted"]["universes"] == 1
    assert applied["deleted"]["character_templates"] == 1
    assert applied["reconcile"]["counts"]["legacy_worlds"] == 0
    assert applied["reconcile"]["counts"]["mingchan_worlds_retained"] == 1

    repeated = apply_cleanup()
    assert not any(repeated["deleted"].values())
    assert repeated["reconcile"]["counts"]["mingchan_worlds_retained"] == 1


def test_cleanup_refuses_legacy_resident_with_weixin_binding(fresh_db):
    user = db.create_or_get_platform_user_by_phone(
        phone="19977000003", display_name="cleanup-blocked"
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="受保护微信账号"
    )["account"]
    _seed_world(
        app_id="zhaoxi",
        suffix="3",
        runtime_account_id=account["id"],
    )
    db.upsert_channel_binding(
        account_id=account["id"],
        channel="openclaw-weixin",
        session_key="cleanup-protected-weixin",
        channel_account_id="bot-test",
        sender_id="sender-test",
        chat_id="user@im.wechat",
        raw_identity={"source": "cleanup-test"},
    )

    plan = build_cleanup_plan()
    assert plan["safe_to_apply"] is False
    assert plan["blockers"]["resident_account_has_weixin_binding"] is True
    with pytest.raises(RuntimeError, match="blocked by protected"):
        apply_cleanup()
    # 失败必须发生在任何 DELETE 之前。
    assert build_cleanup_plan()["counts"]["legacy_worlds"] == 1


def test_preservation_precheck_accepts_protected_zhaoxi_world(fresh_db):
    """保留方案把微信保护项视为必须留存，不再要求删除 legacy World。"""

    user = db.create_or_get_platform_user_by_phone(
        phone="19977000063", display_name="preserved-zhaoxi"
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="保留微信账号"
    )["account"]
    _seed_world(app_id="zhaoxi", suffix="63", runtime_account_id=account["id"])
    db.upsert_channel_binding(
        account_id=account["id"],
        channel="openclaw-weixin",
        session_key="preserved-zhaoxi-weixin",
        channel_account_id="bot-preserved",
        sender_id="sender-preserved",
        chat_id="preserved@im.wechat",
        raw_identity={"source": "preservation-test"},
    )

    report = build_preservation_plan()

    assert report["mode"] == "preserve_legacy_zhaoxi"
    assert report["schema_version"] == 69
    assert report["product_owner_unique"] is True
    assert report["safe_to_enable"] is True
    assert report["legacy_counts_retained"]["legacy_worlds"] == 1
    assert report["legacy_counts_retained"]["protected_weixin_bindings"] == 1
