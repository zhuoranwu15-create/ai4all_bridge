from typing import Any, Dict, List, Optional

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
    channel_account_id: Optional[str] = None
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


class OpenClawDebugTraceRequest(BaseModel):
    trace_id: Optional[str] = None
    channel_account_id: Optional[str] = None
    account_id: Optional[str] = None
    channel: Optional[str] = None
    session_key: Optional[str] = None
    message_id: Optional[str] = None
    source: str = "openclaw"
    llm_model: Optional[str] = None
    system_prompt: Optional[str] = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    reply: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: Optional[int] = None
    error: Optional[str] = None
