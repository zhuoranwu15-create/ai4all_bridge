"""现有 turn runtime 的默认 AgentRuntimePort adapter。"""
from __future__ import annotations

from app.schemas import OpenClawTurnResponse
from app.turn_service import ChannelTurnInput, run_turn_for_account


class DefaultAgentRuntimeAdapter:
    """把形态无关的 turn 输入委派到现有 ``turn_service``。"""

    def send_turn(self, ctx: ChannelTurnInput) -> OpenClawTurnResponse:
        """执行已由上层规范化并注入 context 的一次 turn。"""
        return run_turn_for_account(ctx)
