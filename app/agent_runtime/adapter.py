"""现有 turn runtime 的默认 AgentRuntimePort adapter。"""
from __future__ import annotations

from app.db.companion_world import resolve_conversation_for_owner
from app.schemas import OpenClawTurnResponse
from app.turn_service import ChannelTurnInput, run_turn_for_account


class DefaultAgentRuntimeAdapter:
    """把形态无关端口委派到现有 turn_service，并做 owner-scoped conversation 解析。"""

    def send_turn(self, ctx: ChannelTurnInput) -> OpenClawTurnResponse:
        """执行已由上层规范化并注入 context 的一次 turn。"""
        return run_turn_for_account(ctx)

    def resolve_conversation_account(
        self, conversation_id: str, owner_platform_user_id: str
    ) -> str:
        """解析 owner 自己的 conversation；不存在或越权统一抛 LookupError。"""
        row = resolve_conversation_for_owner(
            conversation_id=conversation_id,
            owner_platform_user_id=owner_platform_user_id,
        )
        if row is None:
            raise LookupError("conversation_not_found")
        return str(row["runtime_account_id"])
