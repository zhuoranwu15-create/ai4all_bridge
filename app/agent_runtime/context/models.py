"""一次 Agent turn 的运行时上下文模型。"""

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
    # per-turn web_search 成功调用计数：动态提醒履约据此校验「至少一次搜索成功」，
    # 避免搜索全失败时仍发出纯模型知识生成的内容。
    web_search_success_count: int = 0
    tdai_search_enabled: bool = False   # 运行时 gating：executor 据此放行两个 TDAI search 工具
    tdai_search_calls: int = 0          # per-turn 计数器：两工具合计调用次数（限流用）
