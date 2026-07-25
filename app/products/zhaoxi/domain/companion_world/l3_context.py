"""L3 共享记忆的 prompt 渲染（域层职责）——M2-B1「读注入」的渲染半刀。

把某世界的 active L3 typed facts 渲染成一个注入用 `ContextBlock`（接缝①）。共享语义与
渲染形态是**产品域层**的组合行为（D-06：绝不编码进 Agent Runtime）；Runtime 只负责把
本函数产出的块喂进 prompt build。payload 是结构化事实、非逐字聊天原文（D-05）。

分层不变量：本文件只 import `app.prompt_builder`（复用 ContextBlock，ADR §7.3 接缝①明示
「域层注入 ContextBlock」），**不** import `app.db.*` / `app.turn_service`——由
tests/test_layer_boundaries.py 门禁执行。读 L3 facts 与 turn 输入组装均在 platform
composition，本层只保留产品渲染规则（D-06）。
"""
import json
from typing import Any, Dict, List, Optional

from app.prompt_builder import ContextBlock

_L3_BLOCK_NAME = "universe_l3"
_L3_HEADER = "【世界共享记忆（关于用户的沉淀认知，同世界居民共享）】"


def _render_payload(payload_json: str) -> str:
    """把一条 fact 的 payload_json 渲染成人读文本；坏 JSON 回退原串。"""
    raw = (payload_json or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw  # 非法 JSON：保留原串，不抛（渲染永不因脏数据崩）
    if isinstance(obj, dict):
        return "，".join(f"{k}：{v}" for k, v in obj.items() if v not in (None, ""))
    if isinstance(obj, list):
        return "、".join(str(x) for x in obj)
    return str(obj)


def render_universe_l3_block(facts: List[Dict[str, Any]]) -> Optional[ContextBlock]:
    """把 active L3 facts 渲染成一个注入用 `ContextBlock`；无可渲染 fact 返回 None（→ 不注入）。

    按 `fact_type` 分组、组内逐条渲染 payload；空/全空则返回 None（空块不注入，保持 no-op）。
    传入顺序即渲染顺序（read 侧已 `created_at ASC`）。
    """
    grouped: "Dict[str, List[str]]" = {}
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

    parts: List[str] = [_L3_HEADER]
    for fact_type in order:
        parts.append(f"[{fact_type}]")
        parts.extend(f"- {line}" for line in grouped[fact_type])
    text = "\n".join(parts)

    return ContextBlock(
        name=_L3_BLOCK_NAME,
        text=text,
        section="volatile",
        trim_priority=35,  # 背景材料级（与 tdai_recall_persona/user_prefs 同档）
    )
