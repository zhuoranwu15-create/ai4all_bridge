"""Plum MVP 请求契约。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: str = Field(min_length=1, max_length=80)


class RedeemAccessCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_code: str = Field(min_length=16, max_length=160)
    display_name: str = Field(min_length=1, max_length=40)


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
