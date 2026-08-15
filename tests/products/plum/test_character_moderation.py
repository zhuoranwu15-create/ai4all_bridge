"""Character moderation plugin boundary."""

import pytest

from app.products.plum.application.character_moderation import (
    CharacterModerationUnavailable,
    character_moderation_plugin,
)


def test_local_plugin_defaults_to_a_deterministic_approved_decision():
    plugin = character_moderation_plugin(app_env="local")
    payload = {"display_name": "Luna", "intro": "A guarded stargazer."}

    first = plugin.review(
        platform_user_id="user_creator",
        normalized_payload=payload,
        creator_declared_rating="general",
    )
    replay = plugin.review(
        platform_user_id="user_creator",
        normalized_payload=payload,
        creator_declared_rating="general",
    )

    assert first == replay
    assert first.status == "approved"
    assert first.effective_rating == "general"
    assert first.decision_id.startswith("localmod_")


def test_production_plugin_fails_closed_until_provider_is_installed():
    plugin = character_moderation_plugin(app_env="production")

    with pytest.raises(
        CharacterModerationUnavailable,
        match="character_moderation_unavailable",
    ):
        plugin.review(
            platform_user_id="user_creator",
            normalized_payload={"display_name": "Luna"},
            creator_declared_rating="mature",
        )
