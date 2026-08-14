"""Application orchestration for one-shot Plum Character creation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, Tuple

from app.products.plum.application.character_moderation import (
    CharacterModerationInput,
    CharacterModerationProvider,
    CharacterModerationStatus,
    CharacterModerationUnavailable,
    get_character_moderation_provider,
)
from app.products.plum.infrastructure.creation_drafts import set_creation_draft_moderation
from app.products.plum.infrastructure.repository import (
    publish_character_revision,
    publish_created_character,
)


@dataclass(frozen=True)
class CreateCharacterCommand:
    """Client-owned fields accepted by the Character creation use case."""

    idempotency_key: str
    display_name: str
    gender: str
    portrait_media_id: str
    portrait_position_x: int
    portrait_position_y: int
    portrait_zoom: int
    avatar_position_x: int
    avatar_position_y: int
    avatar_zoom: int
    intro: str
    opening_scene: str
    character_settings: str
    example_dialogues: str
    response_rules: str
    tag_ids: Tuple[str, ...]
    creator_declared_rating: str
    visibility: str
    adult_confirmed: bool
    rights_confirmed: bool


class CharacterCreationRejected(ValueError):
    """The reviewer made a final rejection decision."""

    def __init__(self, categories: Tuple[str, ...] = ()) -> None:
        super().__init__("character_moderation_rejected")
        self.categories = categories


class CharacterCreationNeedsReview(ValueError):
    """The reviewer could not make an automatic final decision."""


class CharacterCreationConfirmationRequired(ValueError):
    """The creator did not accept the required age or rights declaration."""


CharacterPublisher = Callable[..., Dict[str, object]]


def _normalize(command: CreateCharacterCommand) -> CreateCharacterCommand:
    """Produce the exact text snapshot shared by moderation and persistence."""

    return replace(
        command,
        idempotency_key=command.idempotency_key.strip(),
        display_name=command.display_name.strip(),
        gender=command.gender.strip(),
        portrait_media_id=command.portrait_media_id.strip(),
        intro=command.intro.strip(),
        opening_scene=command.opening_scene.strip(),
        character_settings=command.character_settings.strip(),
        example_dialogues=command.example_dialogues.strip(),
        response_rules=command.response_rules.strip(),
        tag_ids=tuple(tag_id.strip() for tag_id in command.tag_ids),
        creator_declared_rating=command.creator_declared_rating.strip(),
        visibility=command.visibility.strip(),
    )


def create_character(
    *,
    platform_user_id: str,
    command: CreateCharacterCommand,
    provider: CharacterModerationProvider | None = None,
    publisher: CharacterPublisher = publish_created_character,
) -> Dict[str, object]:
    """Review an exact snapshot and atomically publish it only when approved."""

    if not command.adult_confirmed or not command.rights_confirmed:
        raise CharacterCreationConfirmationRequired("character_confirmation_required")

    normalized = _normalize(command)
    reviewer = provider or get_character_moderation_provider()
    decision = reviewer.review(
        CharacterModerationInput(
            request_id=normalized.idempotency_key,
            text_fields={
                "display_name": normalized.display_name,
                "intro": normalized.intro,
                "opening_scene": normalized.opening_scene,
                "character_settings": normalized.character_settings,
                "example_dialogues": normalized.example_dialogues,
                "response_rules": normalized.response_rules,
            },
            portrait_media_id=normalized.portrait_media_id,
            declared_rating=normalized.creator_declared_rating,
        )
    )
    if decision.status is CharacterModerationStatus.REJECTED:
        raise CharacterCreationRejected(decision.categories)
    if decision.status is CharacterModerationStatus.NEEDS_REVIEW:
        raise CharacterCreationNeedsReview("character_moderation_review_required")
    if decision.status is not CharacterModerationStatus.APPROVED:
        raise CharacterModerationUnavailable("character_moderation_result_invalid")
    if not decision.provider_reference or decision.effective_rating not in {
        "general",
        "mature",
    }:
        raise CharacterModerationUnavailable("character_moderation_result_incomplete")

    return publisher(
        platform_user_id=platform_user_id,
        idempotency_key=normalized.idempotency_key,
        display_name=normalized.display_name,
        gender=normalized.gender,
        portrait_media_id=normalized.portrait_media_id,
        portrait_position_x=normalized.portrait_position_x,
        portrait_position_y=normalized.portrait_position_y,
        portrait_zoom=normalized.portrait_zoom,
        avatar_position_x=normalized.avatar_position_x,
        avatar_position_y=normalized.avatar_position_y,
        avatar_zoom=normalized.avatar_zoom,
        intro=normalized.intro,
        opening_scene=normalized.opening_scene,
        character_settings=normalized.character_settings,
        example_dialogues=normalized.example_dialogues,
        response_rules=normalized.response_rules,
        tag_ids=list(normalized.tag_ids),
        creator_declared_rating=normalized.creator_declared_rating,
        approved_moderation_decision_id=decision.provider_reference,
        platform_effective_rating=decision.effective_rating,
        visibility=normalized.visibility,
    )


def submit_creation_draft(
    *, platform_user_id: str, work_id: str, expected_revision: int,
    command: CreateCharacterCommand,
    published_character_id: str = "",
    provider: CharacterModerationProvider | None = None,
) -> Dict[str, object]:
    """Review one saved revision and persist its stable creator-facing status."""

    try:
        character = create_character(
            platform_user_id=platform_user_id,
            command=command,
            provider=provider,
            publisher=(
                (lambda **values: publish_character_revision(
                    **values,
                    work_id=work_id,
                    character_id=published_character_id,
                ))
                if published_character_id
                else (lambda **values: publish_created_character(
                    **values,
                    work_id=work_id,
                    portrait_already_referenced=True,
                ))
            ),
        )
    except CharacterCreationRejected as err:
        draft = set_creation_draft_moderation(
            platform_user_id=platform_user_id,
            work_id=work_id,
            expected_revision=expected_revision,
            status="rejected",
            categories=err.categories,
        )
        return {"moderation_status": "rejected", "draft": draft, "character": None}
    except CharacterCreationNeedsReview:
        draft = set_creation_draft_moderation(
            platform_user_id=platform_user_id,
            work_id=work_id,
            expected_revision=expected_revision,
            status="pending_review",
        )
        return {"moderation_status": "pending_review", "draft": draft, "character": None}

    draft = set_creation_draft_moderation(
        platform_user_id=platform_user_id,
        work_id=work_id,
        expected_revision=expected_revision,
        status="approved",
        provider_reference=str(character.get("moderation_decision_id") or ""),
        published_character_id=str(character.get("character_id") or ""),
    )
    return {"moderation_status": "approved", "draft": draft, "character": character}


__all__ = [
    "CharacterCreationConfirmationRequired",
    "CharacterCreationNeedsReview",
    "CharacterCreationRejected",
    "CreateCharacterCommand",
    "create_character",
    "submit_creation_draft",
]
