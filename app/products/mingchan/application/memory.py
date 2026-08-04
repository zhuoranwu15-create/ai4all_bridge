"""鸣蝉 World typed memory 的写入、sink 构造与批量压缩。"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.agent_runtime.ports import MemoryEvent, MemorySink
from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.config import settings
from app.products.mingchan.domain.memory_sink import MingchanWorldMemorySink
from app.products.mingchan.infrastructure.persistence import companion_world as world_db


def append_mingchan_world_memory_event(event: MemoryEvent) -> None:
    """把鸣蝉 resident 事件追加到所属 World；非鸣蝉账号安全 no-op。"""

    scope = world_db.resolve_resident_memory_scope(
        runtime_account_id=event.provenance.source_account_id,
        expected_app_id=MINGCHAN_APP_ID,
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


def build_mingchan_world_memory_sink() -> Optional[MemorySink]:
    """构造鸣蝉 typed memory sink；L3 后台能力关闭时返回 None。"""

    if not bool(getattr(settings, "mingchan_l3_background_enabled", True)):
        return None
    return MingchanWorldMemorySink(append_mingchan_world_memory_event)


def compact_mingchan_world_memory_batch(
    *, limit: int, after_universe_id: Optional[str] = None
) -> Dict[str, Any]:
    """只扫描并压缩鸣蝉产品内的 World L3 facts。"""

    if not bool(getattr(settings, "mingchan_l3_background_enabled", True)):
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
    "append_mingchan_world_memory_event",
    "build_mingchan_world_memory_sink",
    "compact_mingchan_world_memory_batch",
]
