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
    tdai_search_enabled: bool = False   # 运行时 gating：executor 据此放行两个 TDAI search 工具
    tdai_search_calls: int = 0          # per-turn 计数器：两工具合计调用次数（限流用）
