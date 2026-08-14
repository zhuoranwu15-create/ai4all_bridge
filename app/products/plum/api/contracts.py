"""Plum MVP 请求契约。"""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: str = Field(min_length=1, max_length=80)


class RestartConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)


class RedeemAccessCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(min_length=16, max_length=160)
    display_name: str = Field(min_length=1, max_length=40)


class UpdateGuestProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adult_confirmed: bool
    pronouns: Literal["she_her", "he_him", "they_them", "other"]
    relationship_preference: Optional[Literal[
        "male", "female", "all", "no_preference"
    ]] = None
    genres: List[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def normalize_genres(self):
        cleaned = [str(value).strip() for value in self.genres]
        if any(not value for value in cleaned) or len(cleaned) != len(set(cleaned)):
            raise ValueError("genres must be non-empty and unique")
        self.genres = cleaned
        return self


class CreateEmailChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)


class VerifyEmailChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=8, max_length=100)
    code: str = Field(pattern=r"^\d{6}$")
    preferred_name: Optional[str] = Field(default=None, max_length=40)


class CreateCharacterRequest(BaseModel):
    """Create V1 client input; review outcome and system fields are server-owned."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=50)
    gender: Literal["male", "female", "non_binary"]
    portrait_media_id: str = Field(min_length=1, max_length=100)
    portrait_position_x: int = Field(default=50, ge=0, le=100)
    portrait_position_y: int = Field(default=50, ge=0, le=100)
    portrait_zoom: int = Field(default=100, ge=100, le=200)
    avatar_position_x: int = Field(default=50, ge=0, le=100)
    avatar_position_y: int = Field(default=50, ge=0, le=100)
    avatar_zoom: int = Field(default=100, ge=100, le=200)
    intro: str = Field(min_length=1, max_length=500)
    opening_scene: str = Field(min_length=1, max_length=2000)
    character_settings: str = Field(min_length=1, max_length=12000)
    example_dialogues: str = Field(default="", max_length=6000)
    response_rules: str = Field(default="", max_length=3000)
    tag_ids: List[str] = Field(min_length=1, max_length=5)
    creator_declared_rating: Literal["general", "mature"]
    visibility: Literal["private", "public"] = "private"
    adult_confirmed: bool
    rights_confirmed: bool

    @model_validator(mode="after")
    def require_unique_tags(self):
        """Reject ambiguous duplicate Tag selections before moderation."""

        cleaned = [tag_id.strip() for tag_id in self.tag_ids]
        if any(not tag_id for tag_id in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("tag_ids must be non-empty and unique")
        self.tag_ids = cleaned
        return self


class CreationDraftContent(BaseModel):
    """Incomplete, creator-owned snapshot; review fields remain server-owned."""

    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(default="", max_length=50)
    gender: Literal["", "male", "female", "non_binary"] = ""
    portrait_media_id: str = Field(default="", max_length=100)
    portrait_position_x: int = Field(default=50, ge=0, le=100)
    portrait_position_y: int = Field(default=50, ge=0, le=100)
    portrait_zoom: int = Field(default=100, ge=100, le=200)
    avatar_position_x: int = Field(default=50, ge=0, le=100)
    avatar_position_y: int = Field(default=50, ge=0, le=100)
    avatar_zoom: int = Field(default=100, ge=100, le=200)
    intro: str = Field(default="", max_length=500)
    opening_scene: str = Field(default="", max_length=2000)
    character_settings: str = Field(default="", max_length=12000)
    example_dialogues: str = Field(default="", max_length=6000)
    response_rules: str = Field(default="", max_length=3000)
    tag_ids: List[str] = Field(default_factory=list, max_length=5)
    creator_declared_rating: Literal["general", "mature"] = "general"
    visibility: Literal["private", "public"] = "private"
    adult_confirmed: bool = False
    rights_confirmed: bool = False


class CreateCreationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: CreationDraftContent


class UpdateCreationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    content: CreationDraftContent


class PublishCreationDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class UpdateModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_profile: Literal["fast", "balanced", "immersive"]


class TurnAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["message", "continue"]
    text: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_action_text(self):
        cleaned = str(self.text or "").strip()
        if self.kind == "message" and not cleaned:
            raise ValueError("message text required")
        if self.kind == "continue" and cleaned:
            raise ValueError("continue action cannot include text")
        self.text = cleaned or None
        return self


class CreateTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[str] = Field(default=None, max_length=2000)
    action: Optional[TurnAction] = None
    client_message_id: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def require_matching_request_ids(self):
        """固定计费与 Runtime 去重必须共享同一请求标识。"""

        if self.client_message_id.strip() != self.idempotency_key.strip():
            raise ValueError("client_message_id and idempotency_key must match")
        if self.action is None and not str(self.text or "").strip():
            raise ValueError("text or action required")
        if self.action is not None and self.text is not None:
            raise ValueError("text and action are mutually exclusive")
        return self
