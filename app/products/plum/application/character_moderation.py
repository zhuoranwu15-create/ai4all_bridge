"""Stable Plum Character moderation boundary.

Cloud-vendor request signing, transport fields, and raw responses belong in
provider adapters.  Character creation depends only on the small contract in
this module so changing vendors does not change the HTTP or persistence flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Protocol, Tuple

from app.config import settings


class CharacterModerationStatus(str, Enum):
    """Vendor-neutral outcomes understood by Character creation."""

    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class CharacterModerationInput:
    """Canonical content submitted for one Character review.

    ``text_fields`` keeps the boundary stable if the Create form gains or
    renames fields.  The application service must pass the exact normalized
    values that it will persist after approval.
    """

    request_id: str
    text_fields: Mapping[str, str]
    portrait_media_id: str
    declared_rating: str


@dataclass(frozen=True)
class CharacterModerationResult:
    """Small normalized result returned by every vendor adapter."""

    status: CharacterModerationStatus
    categories: Tuple[str, ...] = field(default_factory=tuple)
    effective_rating: Optional[str] = None
    provider_reference: Optional[str] = None


class CharacterModerationUnavailable(RuntimeError):
    """The configured reviewer cannot produce a trustworthy decision."""


class CharacterModerationProvider(Protocol):
    """Interface implemented by the selected cloud-vendor adapter."""

    def review(self, content: CharacterModerationInput) -> CharacterModerationResult:
        """Review the complete normalized Character payload."""


class UnavailableCharacterModerationProvider:
    """Fail-closed default used until a cloud-vendor adapter is configured."""

    def review(self, content: CharacterModerationInput) -> CharacterModerationResult:
        del content
        raise CharacterModerationUnavailable("character_moderation_not_configured")


_UNAVAILABLE_PROVIDER = UnavailableCharacterModerationProvider()


class LocalMockCharacterModerationProvider:
    """Deterministic local/test adapter; never selected in a deployed environment."""

    def __init__(self, status: str) -> None:
        self.status = status

    def review(self, content: CharacterModerationInput) -> CharacterModerationResult:
        if self.status == "approved":
            return CharacterModerationResult(
                status=CharacterModerationStatus.APPROVED,
                effective_rating=content.declared_rating,
                provider_reference=f"local-mock-{content.request_id}",
            )
        if self.status == "rejected":
            return CharacterModerationResult(
                status=CharacterModerationStatus.REJECTED,
                categories=("mock_policy",),
            )
        return CharacterModerationResult(status=CharacterModerationStatus.NEEDS_REVIEW)


def get_character_moderation_provider() -> CharacterModerationProvider:
    """Resolve the active adapter; vendor configuration will be added here."""

    env = str(settings.app_env or "").strip().lower()
    mock_status = str(settings.plum_character_moderation_mock_status or "").strip()
    if (
        bool(settings.plum_dev_mode)
        and env in {"local", "development", "test"}
        and mock_status in {"approved", "pending_review", "rejected"}
    ):
        return LocalMockCharacterModerationProvider(mock_status)
    return _UNAVAILABLE_PROVIDER


__all__ = [
    "CharacterModerationInput",
    "CharacterModerationProvider",
    "CharacterModerationResult",
    "CharacterModerationStatus",
    "CharacterModerationUnavailable",
    "LocalMockCharacterModerationProvider",
    "get_character_moderation_provider",
]
