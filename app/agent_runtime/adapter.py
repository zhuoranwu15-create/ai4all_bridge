"""现有 turn runtime 的默认 AgentRuntimePort adapter。"""
from __future__ import annotations

from typing import Callable, Optional

from app.channels import CHANNEL_APP, CHANNELS
from app.db.companion_world import resolve_conversation_for_owner
from app.identity import ResolvedIdentity
from app.prompt_builder import ContextBlock
from app.schemas import OpenClawTurnResponse
from app.turn_service import ChannelTurnInput, run_turn_for_account


class DefaultAgentRuntimeAdapter:
    """把形态无关端口委派到现有 turn_service，并做 owner-scoped conversation 解析。"""

    def __init__(
        self,
        universe_context_loader: Optional[
            Callable[[str], Optional[ContextBlock]]
        ] = None,
    ) -> None:
        # loader 由产品 composition root 注入；Runtime 不反向 import app.domains（D-06）。
        self._universe_context_loader = universe_context_loader

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

    def send_companion_world_turn(
        self,
        *,
        conversation_id: str,
        universe_id: str,
        resident_id: str,
        runtime_account_id: str,
        platform_user_id: str,
        sender_name: Optional[str],
        message_id: str,
        text: str,
    ) -> OpenClawTurnResponse:
        """构造 App ChannelTurnInput，注入可选 L3 block 后复用现有 turn 核心。"""
        extra_blocks = []
        if self._universe_context_loader is not None:
            block = self._universe_context_loader(universe_id)
            if block is not None:
                extra_blocks.append(block)
        identity = ResolvedIdentity(
            ai4all_account_id=runtime_account_id,
            session_key=f"app:{conversation_id}",
            channel=CHANNEL_APP,
            channel_account_id=platform_user_id,
            sender_id=platform_user_id,
            chat_id=None,
        )
        return self.send_turn(
            ChannelTurnInput(
                account_id=runtime_account_id,
                cap=CHANNELS[CHANNEL_APP],
                identity=identity,
                message_id=message_id,
                event_id=None,
                message_type="text",
                text=text,
                media=None,
                raw={
                    "source": "companion_world_p1",
                    "conversation_id": conversation_id,
                    "resident_id": resident_id,
                },
                sender_name=sender_name,
                extra_blocks=extra_blocks,
            )
        )
