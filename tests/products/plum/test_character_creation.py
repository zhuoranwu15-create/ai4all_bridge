"""Unit tests for provider-neutral Plum Character creation orchestration."""

import pytest

from app.products.plum.api.contracts import CreateCharacterRequest
from app.products.plum.application.character_creation import (
    CharacterCreationConfirmationRequired,
    CharacterCreationNeedsReview,
    CharacterCreationRejected,
    CreateCharacterCommand,
    create_character,
    submit_creation_draft,
)
from app.products.plum.application.character_moderation import (
    CharacterModerationResult,
    CharacterModerationStatus,
    CharacterModerationUnavailable,
)


def _command(**overrides) -> CreateCharacterCommand:
    values = {
        "idempotency_key": "  create-request-123  ",
        "display_name": "  Luna  ",
        "gender": "female",
        "portrait_media_id": "  media-portrait-123  ",
        "portrait_position_x": 45,
        "portrait_position_y": 55,
        "portrait_zoom": 135,
        "avatar_position_x": 40,
        "avatar_position_y": 60,
        "avatar_zoom": 125,
        "intro": "  A careful listener.  ",
        "opening_scene": "  Luna looks up.  ",
        "character_settings": "  Patient and observant.  ",
        "example_dialogues": "  Hello.  ",
        "response_rules": "  Stay in character.  ",
        "tag_ids": (" tag-calm ",),
        "creator_declared_rating": "general",
        "visibility": "private",
        "adult_confirmed": True,
        "rights_confirmed": True,
    }
    values.update(overrides)
    return CreateCharacterCommand(**values)


class _Provider:
    def __init__(self, result: CharacterModerationResult) -> None:
        self.result = result
        self.input = None

    def review(self, content):
        self.input = content
        return self.result


def test_approved_snapshot_is_identical_for_review_and_publish():
    provider = _Provider(
        CharacterModerationResult(
            status=CharacterModerationStatus.APPROVED,
            effective_rating="general",
            provider_reference="cloud-decision-123",
        )
    )
    published = {}

    def publisher(**values):
        published.update(values)
        return {"id": "character-123"}

    result = create_character(
        platform_user_id="user-123",
        command=_command(),
        provider=provider,
        publisher=publisher,
    )

    assert result == {"id": "character-123"}
    assert provider.input.request_id == "create-request-123"
    assert provider.input.text_fields["display_name"] == "Luna"
    assert provider.input.text_fields["intro"] == "A careful listener."
    assert published["display_name"] == provider.input.text_fields["display_name"]
    assert published["intro"] == provider.input.text_fields["intro"]
    assert published["tag_ids"] == ["tag-calm"]
    assert published["avatar_zoom"] == 125
    assert published["portrait_zoom"] == 135
    assert published["approved_moderation_decision_id"] == "cloud-decision-123"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (CharacterModerationStatus.REJECTED, CharacterCreationRejected),
        (CharacterModerationStatus.NEEDS_REVIEW, CharacterCreationNeedsReview),
    ],
)
def test_non_approved_decision_never_publishes(status, error):
    provider = _Provider(CharacterModerationResult(status=status))

    with pytest.raises(error):
        create_character(
            platform_user_id="user-123",
            command=_command(),
            provider=provider,
            publisher=lambda **values: pytest.fail(f"published unexpectedly: {values}"),
        )


def test_required_confirmations_are_checked_before_review():
    provider = _Provider(CharacterModerationResult(CharacterModerationStatus.APPROVED))

    with pytest.raises(CharacterCreationConfirmationRequired):
        create_character(
            platform_user_id="user-123",
            command=_command(rights_confirmed=False),
            provider=provider,
        )

    assert provider.input is None


def test_incomplete_approved_result_fails_closed():
    provider = _Provider(
        CharacterModerationResult(status=CharacterModerationStatus.APPROVED)
    )

    with pytest.raises(
        CharacterModerationUnavailable,
        match="character_moderation_result_incomplete",
    ):
        create_character(
            platform_user_id="user-123",
            command=_command(),
            provider=provider,
        )


def test_saved_draft_pending_review_is_persisted_without_publish(monkeypatch):
    from app.products.plum.application import character_creation as application

    provider = _Provider(
        CharacterModerationResult(status=CharacterModerationStatus.NEEDS_REVIEW)
    )
    recorded = {}
    monkeypatch.setattr(
        application,
        "set_creation_draft_moderation",
        lambda **values: recorded.update(values) or {"work_id": values["work_id"]},
    )
    monkeypatch.setattr(
        application,
        "publish_created_character",
        lambda **values: pytest.fail(f"published unexpectedly: {values}"),
    )

    result = submit_creation_draft(
        platform_user_id="user-123",
        work_id="work-123",
        expected_revision=4,
        command=_command(),
        provider=provider,
    )

    assert result["moderation_status"] == "pending_review"
    assert result["character"] is None
    assert recorded["status"] == "pending_review"
    assert recorded["expected_revision"] == 4


def test_saved_draft_approval_uses_stable_work_and_referenced_portrait(monkeypatch):
    from app.products.plum.application import character_creation as application

    provider = _Provider(
        CharacterModerationResult(
            status=CharacterModerationStatus.APPROVED,
            effective_rating="general",
            provider_reference="review-123",
        )
    )
    published = {}
    recorded = {}
    monkeypatch.setattr(
        application,
        "publish_created_character",
        lambda **values: published.update(values) or {
            "work_id": values["work_id"],
            "character_id": "char-123",
            "moderation_decision_id": values["approved_moderation_decision_id"],
        },
    )
    monkeypatch.setattr(
        application,
        "set_creation_draft_moderation",
        lambda **values: recorded.update(values) or {"work_id": values["work_id"]},
    )

    result = submit_creation_draft(
        platform_user_id="user-123",
        work_id="work-123",
        expected_revision=2,
        command=_command(),
        provider=provider,
    )

    assert result["moderation_status"] == "approved"
    assert published["work_id"] == "work-123"
    assert published["portrait_already_referenced"] is True
    assert recorded["published_character_id"] == "char-123"
    assert recorded["provider_reference"] == "review-123"


def test_published_draft_approval_creates_next_version_not_another_character(monkeypatch):
    from app.products.plum.application import character_creation as application

    provider = _Provider(CharacterModerationResult(
        status=CharacterModerationStatus.APPROVED,
        effective_rating="general",
        provider_reference="review-version-2",
    ))
    revised = {}
    monkeypatch.setattr(
        application,
        "publish_character_revision",
        lambda **values: revised.update(values) or {
            "character_id": values["character_id"],
            "moderation_decision_id": values["approved_moderation_decision_id"],
        },
    )
    monkeypatch.setattr(
        application,
        "publish_created_character",
        lambda **values: pytest.fail(f"created another character: {values}"),
    )
    monkeypatch.setattr(
        application,
        "set_creation_draft_moderation",
        lambda **values: {"work_id": values["work_id"]},
    )

    result = submit_creation_draft(
        platform_user_id="user-123",
        work_id="work-123",
        expected_revision=5,
        command=_command(),
        published_character_id="char-123",
        provider=provider,
    )

    assert result["moderation_status"] == "approved"
    assert revised["work_id"] == "work-123"
    assert revised["character_id"] == "char-123"


def test_create_character_name_limit_matches_create_form():
    accepted = CreateCharacterRequest(
        idempotency_key="create-request-123",
        display_name="A" * 50,
        gender="female",
        portrait_media_id="media-portrait-123",
        intro="A careful listener.",
        opening_scene="Luna looks up.",
        character_settings="Patient and observant.",
        tag_ids=["tag-calm"],
        creator_declared_rating="general",
        visibility="private",
        adult_confirmed=True,
        rights_confirmed=True,
    )

    assert len(accepted.display_name) == 50
    with pytest.raises(ValueError):
        CreateCharacterRequest(**{**accepted.model_dump(), "display_name": "A" * 51})
