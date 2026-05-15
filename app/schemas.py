from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class MediaPayload(BaseModel):
    media_id: Optional[str] = None
    url: Optional[str] = None
    path: Optional[str] = None
    format: Optional[str] = None
    duration_ms: Optional[int] = None


class OpenClawTurnRequest(BaseModel):
    event_id: Optional[str] = None
    message_id: Optional[str] = None
    channel: Optional[str] = None
    account_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    chat_id: Optional[str] = None
    chat_type: str = "private"
    session_key: Optional[str] = None
    message_type: str = "text"
    text: Optional[str] = None
    media: Optional[MediaPayload] = None
    timestamp: Optional[int] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class OpenClawTurnResponse(BaseModel):
    status: str
    reply: Optional[str] = None
    no_reply: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
