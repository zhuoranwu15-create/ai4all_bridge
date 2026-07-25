"""现有 turn runtime 的默认 AgentRuntimePort adapter。"""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas import OpenClawTurnResponse
from app.agent_runtime.turns.contracts import ProductTurnServices
from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn


@dataclass(frozen=True)
class DefaultAgentRuntimeAdapter:
    """把形态无关的 turn 输入委派到现有 ``turn_service``。"""

    product_services: ProductTurnServices

    def send_turn(self, ctx: ChannelTurnInput) -> OpenClawTurnResponse:
        """执行已由上层规范化并注入 context 的一次 turn。"""
        return run_product_turn(ctx, product_services=self.product_services)
