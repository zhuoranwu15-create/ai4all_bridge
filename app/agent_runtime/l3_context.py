"""L3 共享上下文读 facade（Runtime 层）——M2-B1「读注入」的读半刀。

组合「读某世界 active L3 facts（app.db）+ 域层渲染成 ContextBlock」。Runtime 层允许直接
依赖 app.db（是域层→Runtime 的受管接缝），故 I/O 落此处、渲染语义委托域层
（`app.domains.companion_world.l3_context.render_universe_l3_block`，D-06）。
"""
from typing import Optional

from app.db._backend import Connection
from app.db.companion_world import read_universe_facts
from app.domains.companion_world.l3_context import render_universe_l3_block
from app.prompt_builder import ContextBlock


def read_universe_context(
    *, universe_id: str, conn: Optional[Connection] = None
) -> Optional[ContextBlock]:
    """读某世界 active L3 facts 并渲染成注入用 `ContextBlock`；无 fact/空世界返回 None。

    ADR §12「L3 首刀」的 `read_universe_context()`。**只读 `status='active'`**（compact 后
    superseded 行不注入）、严格按 `universe_id` 锚（跨 universe 不可见，read_universe_facts 保证）。

    本刀（M2-B1）**无 live 调用方**——form-A（微信）不调、退化 1:1；form-B 的 App turn 适配器
    随 M2-C 调用并把结果填入 `ChannelTurnInput.extra_blocks`（接缝①）。account→universe 解析
    留调用方（M2-C 由 conversation_id 定位 universe），本 facade 只吃 universe_id。
    """
    facts = read_universe_facts(universe_id=universe_id, status="active", conn=conn)
    return render_universe_l3_block(facts)
