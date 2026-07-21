"""L3 共享记忆的 prompt 渲染（域层职责）——M2-B1「读注入」的渲染半刀。

把某世界的 active L3 typed facts 渲染成一个注入用 `ContextBlock`（接缝①）。共享语义与
渲染形态是**产品域层**的组合行为（D-06：绝不编码进 Agent Runtime）；Runtime 只负责把
本函数产出的块喂进 prompt build。payload 是结构化事实、非逐字聊天原文（D-05）。

分层不变量：本文件只 import `app.prompt_builder`（复用 ContextBlock，ADR §7.3 接缝①明示
「域层注入 ContextBlock」）与 `app.agent_runtime.*`（经 Runtime 读接缝取 L3 facts，域层→
agent_runtime 合法），**不** import `app.db.*` / `app.turn_service`——由
tests/test_layer_boundaries.py 门禁执行。「读 L3 facts」的 I/O 在 Runtime 侧
（app.agent_runtime.l3_context），本层负责「读+渲染」的产品组合（D-06）。
"""
import json
from typing import Any, Dict, List, Optional

from app.agent_runtime.l3_context import read_active_universe_context_facts
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


def read_universe_context(
    *, universe_id: str, conn: Optional[Any] = None
) -> Optional[ContextBlock]:
    """读某世界 active L3 facts 并渲染成注入用 `ContextBlock`；无 fact/空世界返回 None。

    ADR §12「L3 首刀」的 `read_universe_context()`——「读+渲染」的产品组合（域层职责，D-06）：
    读经 Runtime 读接缝（`app.agent_runtime.l3_context.read_active_universe_context_facts`，
    严格按 universe_id 锚、只取 status='active'），渲染用本模块 `render_universe_l3_block`。

    本刀（M2-B1）**无 live 调用方**——form-A（微信）不调、退化 1:1；form-B 的 App turn 适配器
    随 M2-C 调用并把结果填入 `ChannelTurnInput.extra_blocks`（接缝①）。account→universe 解析
    留调用方（M2-C 由 conversation_id 定位 universe），本组合只吃 universe_id。
    """
    facts = read_active_universe_context_facts(universe_id=universe_id, conn=conn)
    return render_universe_l3_block(facts)
