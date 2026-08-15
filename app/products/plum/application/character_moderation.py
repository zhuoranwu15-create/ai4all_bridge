"""Pluggable moderation boundary for user-created Plum Characters."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol


class CharacterModerationUnavailable(RuntimeError):
    """No trusted Character moderation plugin is available in this environment."""


@dataclass(frozen=True)
class CharacterModerationDecision:
    decision_id: str
    status: Literal["approved", "rejected"]
    effective_rating: Literal["general", "mature"]


class CharacterModerationPlugin(Protocol):
    def review(
        self,
        *,
        platform_user_id: str,
        normalized_payload: Mapping[str, object],
        creator_declared_rating: Literal["general", "mature"],
    ) -> CharacterModerationDecision: ...


class LocalAutoApproveCharacterModeration:
    """Development plugin that deterministically approves the exact payload."""

    def review(
        self,
        *,
        platform_user_id: str,
        normalized_payload: Mapping[str, object],
        creator_declared_rating: Literal["general", "mature"],
    ) -> CharacterModerationDecision:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "plugin": "local-auto-approve-v1",
                    "platform_user_id": platform_user_id,
                    **normalized_payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        return CharacterModerationDecision(
            decision_id=f"localmod_{digest}",
            status="approved",
            effective_rating=creator_declared_rating,
        )


class UnavailableCharacterModeration:
    """Production placeholder that fails closed until a provider is installed."""

    def review(
        self,
        *,
        platform_user_id: str,
        normalized_payload: Mapping[str, object],
        creator_declared_rating: Literal["general", "mature"],
    ) -> CharacterModerationDecision:
        del platform_user_id, normalized_payload, creator_declared_rating
        raise CharacterModerationUnavailable("character_moderation_unavailable")


def character_moderation_plugin(*, app_env: str) -> CharacterModerationPlugin:
    """Resolve the installed plugin without letting production auto-approve."""

    if str(app_env or "").strip().lower() in {"local", "development", "test"}:
        return LocalAutoApproveCharacterModeration()
    return UnavailableCharacterModeration()


__all__ = [
    "CharacterModerationDecision",
    "CharacterModerationPlugin",
    "CharacterModerationUnavailable",
    "LocalAutoApproveCharacterModeration",
    "UnavailableCharacterModeration",
    "character_moderation_plugin",
]
