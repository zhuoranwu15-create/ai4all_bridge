"""Plum MVP 请求契约。"""
from typing import List, Literal

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


class CreateCharacterRequest(BaseModel):
    """Create V1 client input; review outcome and system fields are server-owned."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)
    gender: Literal["male", "female", "non_binary"]
    portrait_media_id: str = Field(min_length=1, max_length=100)
    portrait_position_x: int = Field(default=50, ge=0, le=100)
    portrait_position_y: int = Field(default=50, ge=0, le=100)
    avatar_position_x: int = Field(default=50, ge=0, le=100)
    avatar_position_y: int = Field(default=50, ge=0, le=100)
    intro: str = Field(min_length=1, max_length=500)
    opening_scene: str = Field(min_length=1, max_length=2000)
    character_settings: str = Field(min_length=1, max_length=12000)
    example_dialogues: str = Field(default="", max_length=6000)
    response_rules: str = Field(default="", max_length=3000)
    tag_ids: List[str] = Field(min_length=1, max_length=5)
    creator_declared_rating: Literal["general", "mature"]
    visibility: Literal["private", "public"] = "private"

    @model_validator(mode="after")
    def require_unique_tags(self):
        """Reject ambiguous duplicate Tag selections before moderation."""

        cleaned = [tag_id.strip() for tag_id in self.tag_ids]
        if any(not tag_id for tag_id in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("tag_ids must be non-empty and unique")
        self.tag_ids = cleaned
        return self


class UpdateModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_profile: Literal["fast", "balanced", "immersive"]


class CreateTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def require_matching_request_ids(self):
        """固定计费与 Runtime 去重必须共享同一请求标识。"""

        if self.client_message_id.strip() != self.idempotency_key.strip():
            raise ValueError("client_message_id and idempotency_key must match")
        return self
