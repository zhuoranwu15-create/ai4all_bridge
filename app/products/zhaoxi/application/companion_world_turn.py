"""Companion World turn 的产品 composition adapter。"""
from __future__ import annotations

from typing import Optional

from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.config import settings
from app.products.zhaoxi.infrastructure.persistence.companion_world import read_universe_facts
from app.products.zhaoxi.domain.companion_world.l3_context import render_universe_l3_block
from app.platform.auth.identity import ResolvedIdentity
from app.prompt_builder import ContextBlock
from app.schemas import OpenClawTurnResponse
from app.agent_runtime.turns.service import ChannelTurnInput

from app.products.zhaoxi.application.companion_world_memory import build_companion_world_memory_sink
from app.products.zhaoxi.application.turn_services import ZHAOXI_TURN_SERVICES


def read_companion_world_context(universe_id: str) -> Optional[ContextBlock]:
    """读取一个世界的 active L3 facts，并按产品规则渲染为 prompt block。"""
    if not bool(getattr(settings, "companion_world_l3_background_enabled", True)):
        return None
    facts = read_universe_facts(universe_id=universe_id, status="active")
    return render_universe_l3_block(facts)


def run_companion_world_turn(
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
    """组装 World/App 输入与共享 context，再委派给形态无关 Runtime。"""
    block = read_companion_world_context(universe_id)
    identity = ResolvedIdentity(
        ai4all_account_id=runtime_account_id,
        session_key=f"app:{conversation_id}",
        channel=CHANNEL_APP,
        channel_account_id=platform_user_id,
        sender_id=platform_user_id,
        chat_id=None,
    )
    return DefaultAgentRuntimeAdapter(ZHAOXI_TURN_SERVICES).send_turn(
        ChannelTurnInput(
            account_id=runtime_account_id,
            app_id=ZHAOXI_APP_ID,
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
            extra_blocks=[block] if block is not None else [],
            memory_sink=build_companion_world_memory_sink(),
        )
    )


__all__ = ["read_companion_world_context", "run_companion_world_turn"]
