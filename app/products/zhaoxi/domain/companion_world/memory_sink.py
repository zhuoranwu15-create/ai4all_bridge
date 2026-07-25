"""Companion World 的 typed memory 路由策略（不含 I/O）。"""
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


class CompanionWorldMemorySink(MemorySink):
    """按 fact_type 把用户沉淀事实路由到注入的 L3 writer。

    关系与承诺继续留在 per-account L2；未知类型直接拒绝，防止新类型在没有
    明确共享评审时意外扩散到同世界其他 resident。
    """

    def __init__(self, writer: Callable[[MemoryEvent], None]) -> None:
        self._writer = writer

    def emit(self, event: MemoryEvent) -> None:
        """路由一条结构化记忆事件；未知类型 fail-closed。"""
        fact_type = str(event.fact_type or "").strip()
        if fact_type in L2_FACT_TYPES:
            return
        if fact_type not in L3_FACT_TYPES:
            raise ValueError("unsupported_memory_fact_type")
        self._writer(event)


__all__ = ["CompanionWorldMemorySink", "L2_FACT_TYPES", "L3_FACT_TYPES"]
