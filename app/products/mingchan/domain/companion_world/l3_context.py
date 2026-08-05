"""鸣蝉 World L3 共享事实的 Prompt 渲染规则。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.agent_runtime.context.prompt_builder import ContextBlock

_L3_HEADER = "【世界共享记忆（关于用户的沉淀认知，同世界居民共享）】"


def _render_payload(payload_json: str) -> str:
    """把结构化事实渲染为简洁文本；坏 JSON 保留原始内容。"""

    raw = str(payload_json or "").strip()
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if isinstance(value, dict):
        return "，".join(
            f"{key}：{item}"
            for key, item in value.items()
            if item not in (None, "")
        )
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def render_universe_l3_block(
    facts: List[Dict[str, Any]],
) -> Optional[ContextBlock]:
    """按 fact_type 稳定渲染 active World facts；无内容时返回 None。"""

    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for fact in facts or []:
        fact_type = str(fact.get("fact_type") or "").strip()
        line = _render_payload(str(fact.get("payload_json") or ""))
        if not fact_type or not line:
            continue
        if fact_type not in grouped:
            grouped[fact_type] = []
            order.append(fact_type)
        grouped[fact_type].append(line)
    if not order:
        return None

    parts = [_L3_HEADER]
    for fact_type in order:
        parts.append(f"[{fact_type}]")
        parts.extend(f"- {line}" for line in grouped[fact_type])
    return ContextBlock(
        name="universe_l3",
        text="\n".join(parts),
        section="volatile",
        trim_priority=35,
    )


__all__ = ["render_universe_l3_block"]
