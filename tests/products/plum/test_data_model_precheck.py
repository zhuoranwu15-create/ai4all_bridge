"""Plum 数据模型迁移 72–75 的只读预检。"""

from unittest.mock import patch

import app.db as db
from scripts.precheck_plum_data_model import run_precheck


def test_clean_current_plum_schema_passes_read_only_precheck(fresh_db):
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        report = run_precheck(conn)
        after = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]

    assert report.blocking() == []
    assert before == after
    assert "plum_characters" in report.inventory
    assert all(not hasattr(check, "rows") for check in report.checks)


def test_precheck_blocks_rating_and_runtime_owner_drift_before_m0072(
    test_settings, empty_pg_database
):
    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=71, expected_current_version=0)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_precheck_a', 'plum-precheck-a@local.invalid', 'A'),
                       ('pusr_precheck_b', 'plum-precheck-b@local.invalid', 'B')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_characters(
                    id, display_name, tagline, intro, greeting, persona_prompt,
                    status, content_rating
                ) VALUES ('char_precheck', 'Precheck', 'tagline', 'intro',
                          'hello', 'settings', 'active', '')
                """
            )
            conn.execute(
                """
                INSERT INTO accounts(id, display_name, app_id)
                VALUES ('aid_precheck_plum', 'Runtime', 'plum')
                """
            )
            conn.execute(
                """
                INSERT INTO runtime_ownerships(
                    runtime_account_id, platform_user_id, app_id,
                    source_type, source_id
                ) VALUES ('aid_precheck_plum', 'pusr_precheck_b', 'plum',
                          'character_binding', 'wrong-owner')
                """
            )
            conn.execute(
                """
                INSERT INTO plum_character_bindings(
                    platform_user_id, character_id, runtime_account_id,
                    character_prompt_version
                ) VALUES ('pusr_precheck_a', 'char_precheck',
                          'aid_precheck_plum', 1)
                """
            )

            report = run_precheck(conn)

    totals = {check.name: check.total for check in report.checks}
    assert totals["invalid_character_rating_projection"] == 1
    assert totals["legacy_binding_runtime_scope_drift"] == 1
    assert {check.name for check in report.blocking()} >= {
        "invalid_character_rating_projection",
        "legacy_binding_runtime_scope_drift",
    }
    db.close_pg_pool()
