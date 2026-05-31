import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class TurnContext:
    account_id: str
    account: dict
    session: dict
    identity: Any                     # OpenClawIdentity
    binding: dict
    message_id: str
    text: str
    today: str
    business_day: str
    profile_path: Optional[Path]
    debug_trace_enabled: bool
    onboarding_state: str
    onboarding_active: bool
    recent_messages: List[dict] = field(default_factory=list)
    background_loop: Optional[asyncio.AbstractEventLoop] = None
    web_search_enabled: bool = False
