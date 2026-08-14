"""Pure contract tests for the pluggable Plum Character reviewer."""

import pytest

from app.products.plum.application.character_moderation import (
    CharacterModerationInput,
    CharacterModerationResult,
    CharacterModerationStatus,
    CharacterModerationUnavailable,
    get_character_moderation_provider,
)


def _input() -> CharacterModerationInput:
    return CharacterModerationInput(
        request_id="create-request-123",
        text_fields={"display_name": "Luna", "intro": "A careful listener."},
        portrait_media_id="media-portrait-123",
        declared_rating="general",
    )


def test_character_moderation_statuses_are_vendor_neutral_and_stable():
    assert {status.value for status in CharacterModerationStatus} == {
        "approved",
        "rejected",
        "needs_review",
    }


def test_provider_contract_accepts_a_normalized_result():
    class FakeProvider:
        def review(self, content: CharacterModerationInput) -> CharacterModerationResult:
            assert content == _input()
            return CharacterModerationResult(
                status=CharacterModerationStatus.APPROVED,
                effective_rating="general",
                provider_reference="fake-decision-123",
            )

    result = FakeProvider().review(_input())

    assert result.status is CharacterModerationStatus.APPROVED
    assert result.effective_rating == "general"


def test_unconfigured_provider_fails_closed():
    with pytest.raises(
        CharacterModerationUnavailable,
        match="character_moderation_not_configured",
    ):
        get_character_moderation_provider().review(_input())


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("approved", CharacterModerationStatus.APPROVED),
        ("pending_review", CharacterModerationStatus.NEEDS_REVIEW),
        ("rejected", CharacterModerationStatus.REJECTED),
    ],
)
def test_local_mock_provider_exposes_stable_statuses(monkeypatch, configured, expected):
    from app.products.plum.application import character_moderation as moderation

    monkeypatch.setattr(moderation.settings, "app_env", "local")
    monkeypatch.setattr(moderation.settings, "plum_dev_mode", True)
    monkeypatch.setattr(
        moderation.settings, "plum_character_moderation_mock_status", configured
    )

    assert moderation.get_character_moderation_provider().review(_input()).status is expected


def test_production_ignores_mock_moderation_configuration(monkeypatch):
    from app.products.plum.application import character_moderation as moderation

    monkeypatch.setattr(moderation.settings, "app_env", "production")
    monkeypatch.setattr(moderation.settings, "plum_dev_mode", True)
    monkeypatch.setattr(
        moderation.settings, "plum_character_moderation_mock_status", "approved"
    )

    with pytest.raises(CharacterModerationUnavailable):
        moderation.get_character_moderation_provider().review(_input())
