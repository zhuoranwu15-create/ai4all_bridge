"""Companion World typed memory 的 SQL 写入与 compact 组合。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.agent_runtime.ports import MemoryEvent, MemorySink
from app.config import settings
from app.products.zhaoxi.domain.companion_world.memory_sink import CompanionWorldMemorySink
from app.products.zhaoxi.infrastructure.persistence import companion_world as world_db


def append_companion_world_memory_event(event: MemoryEvent) -> None:
    """把 resident 来源事件追加到其所属 universe；form-A account 安全 no-op。"""
    scope = world_db.resolve_resident_memory_scope(
        runtime_account_id=event.provenance.source_account_id
    )
    if scope is None:
        return
    world_db.append_universe_fact(
        universe_id=str(scope["universe_id"]),
        fact_type=event.fact_type,
        payload_json=json.dumps(
            event.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        source_account_id=event.provenance.source_account_id,
        source_resident_id=str(scope["resident_id"]),
        source_message_id=event.provenance.turn_message_id,
        occurred_at=event.provenance.occurred_at,
    )


def build_companion_world_memory_sink() -> Optional[MemorySink]:
    """构造生产用 typed memory sink；后台 L3 开关关闭时返回 None。"""
    if not bool(getattr(settings, "companion_world_l3_background_enabled", True)):
        return None
    return CompanionWorldMemorySink(append_companion_world_memory_event)


def compact_companion_world_memory_batch(
    *, limit: int, after_universe_id: Optional[str] = None
) -> Dict[str, Any]:
    """按稳定游标压缩一批 universe，供单例 DreamingScheduler 调用。"""
    if not bool(getattr(settings, "companion_world_l3_background_enabled", True)):
        return {
            "scanned": 0,
            "merged_groups": 0,
            "superseded_facts": 0,
            "next_cursor": None,
            "results": [],
            "disabled": True,
        }
    universe_ids, next_cursor = world_db.list_universe_ids_for_memory_compact(
        after_universe_id=after_universe_id,
        limit=limit,
    )
    results = [
        world_db.compact_universe_facts(universe_id=universe_id)
        for universe_id in universe_ids
    ]
    return {
        "scanned": len(universe_ids),
        "merged_groups": sum(int(item["merged_groups"]) for item in results),
        "superseded_facts": sum(
            int(item["superseded_facts"]) for item in results
        ),
        "next_cursor": next_cursor,
        "results": results,
    }


__all__ = [
    "append_companion_world_memory_event",
    "build_companion_world_memory_sink",
    "compact_companion_world_memory_batch",
]
