"""L3 共享上下文的 Runtime 侧读接缝——M2-B1「读注入」的读半刀（**仅 I/O**）。

只负责「读某世界 active L3 facts」这一形态无关的 I/O（Runtime 层允许直接依赖 app.db，是
域层→Runtime 的受管接缝）。**渲染语义与「读+渲染」的组合属产品域层**（D-06：绝不编码进
Agent Runtime），故本模块**不** import `app.domains.*`——组合在
`app.domains.companion_world.l3_context.read_universe_context`（域层→agent_runtime 合法，
依赖方向见 ADR §依赖方向 L181：`域层 → AgentRuntimePort → 现有实现`）。
"""
from typing import Any, Dict, List, Optional

from app.db._backend import Connection
from app.db.companion_world import read_universe_facts


def read_active_universe_context_facts(
    *, universe_id: str, conn: Optional[Connection] = None
) -> List[Dict[str, Any]]:
    """读某世界 `status='active'` 的 L3 typed facts（严格按 universe_id 锚，跨 universe 不可见）。

    形态无关的纯读：返回结构化 fact 行、不含任何渲染/共享语义（那是域层职责）。compact 后
    superseded 行不返回。account→universe 的解析留调用方（M2-C 由 conversation_id 定位）。
    """
    return read_universe_facts(universe_id=universe_id, status="active", conn=conn)
