"""full_system_reset 的清理边界：用户态必须覆盖当前产品 schema。"""

from scripts.full_system_reset import TABLES_TO_CLEAR, TABLES_TO_KEEP


def test_full_reset_clears_product_user_truth_and_moderation_state():
    required = {
        "account_profile_files",
        "product_memberships",
        "content_moderation_tasks",
        "content_moderation_results",
        "content_moderation_actions",
        "content_moderation_exports",
        "moderation_account_risk_state",
        "universes",
        "universe_residents",
        "universe_posts",
        "universe_visits",
        "resident_lifecycle_events",
        "resident_wishes",
        "human_conversations",
        "companion_world_outbox",
        "fibre_conversations",
        "fibre_user_personas",
        "runtime_turn_runs",
    }

    assert required.issubset(set(TABLES_TO_CLEAR))
    assert not required & TABLES_TO_KEEP


def test_full_reset_does_not_clear_static_catalog_or_admin_tables():
    assert {
        "admin_users",
        "admin_access_events",
        "character_templates",
        "campaign_codes",
        "llm_runtime_config",
    }.issubset(TABLES_TO_KEEP)
    assert not TABLES_TO_KEEP & set(TABLES_TO_CLEAR)
