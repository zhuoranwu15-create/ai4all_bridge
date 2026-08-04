"""鸣蝉 World typed memory 的纯路由策略。"""
from __future__ import annotations

from typing import Callable

from app.agent_runtime.ports import MemoryEvent, MemorySink

L3_FACT_TYPES = frozenset(
    {
        "user_identity",
        "user_preference",
        "user_profile_derived",
        "user_event",
    }
)
L2_FACT_TYPES = frozenset({"relationship", "commitment"})


class MingchanWorldMemorySink(MemorySink):
    """只把允许跨居民共享的事实发送给鸣蝉 L3 writer。"""

    def __init__(self, writer: Callable[[MemoryEvent], None]) -> None:
        self._writer = writer

    def emit(self, event: MemoryEvent) -> None:
        """路由结构化记忆事件；未知类型 fail closed。"""

        fact_type = str(event.fact_type or "").strip()
        if fact_type in L2_FACT_TYPES:
            return
        if fact_type not in L3_FACT_TYPES:
            raise ValueError("unsupported_memory_fact_type")
        self._writer(event)


__all__ = ["L2_FACT_TYPES", "L3_FACT_TYPES", "MingchanWorldMemorySink"]
