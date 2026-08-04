"""鸣蝉 Companion World 居民 turn 的产品 composition adapter。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter
from app.agent_runtime.context.prompt_builder import ContextBlock
from app.agent_runtime.turns.service import ChannelTurnInput
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import read_universe_facts
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.products.mingchan.application.turn_services import MingchanTurnServices
from app.products.mingchan.application.memory import build_mingchan_world_memory_sink
from app.products.mingchan.domain.companion_world.l3_context import (
    render_universe_l3_block,
)
from app.schemas import MediaPayload, OpenClawTurnResponse


def read_companion_world_context(universe_id: str) -> Optional[ContextBlock]:
    """读取并按鸣蝉规则渲染一个 World 的 active L3 facts。"""

    if not bool(getattr(settings, "mingchan_l3_background_enabled", True)):
        return None
    facts = read_universe_facts(universe_id=universe_id, status="active")
    return render_universe_l3_block(facts)


def run_mingchan_companion_world_turn(
    *,
    conversation_id: str,
    universe_id: str,
    resident_id: str,
    runtime_account_id: str,
    platform_user_id: str,
    sender_name: Optional[str],
    message_id: str,
    text: str,
    message_type: str = "text",
    media: Optional[MediaPayload] = None,
    display_content: Optional[Dict[str, Any]] = None,
    media_asset_id: Optional[str] = None,
    media_asset_owner_id: Optional[str] = None,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> OpenClawTurnResponse:
    """固定鸣蝉 audience 组装居民输入并委派给形态无关 Runtime。"""

    block = read_companion_world_context(universe_id)
    identity = ResolvedIdentity(
        ai4all_account_id=runtime_account_id,
        session_key=f"app:{conversation_id}",
        channel=CHANNEL_APP,
        channel_account_id=platform_user_id,
        sender_id=platform_user_id,
        chat_id=None,
    )
    return DefaultAgentRuntimeAdapter(MingchanTurnServices(registry)).send_turn(
        ChannelTurnInput(
            account_id=runtime_account_id,
            app_id=MINGCHAN_APP_ID,
            cap=CHANNELS[CHANNEL_APP],
            identity=identity,
            message_id=message_id,
            event_id=None,
            message_type=message_type,
            text=text,
            media=media,
            raw={
                "source": "mingchan_companion_world",
                "conversation_id": conversation_id,
                "resident_id": resident_id,
            },
            sender_name=sender_name,
            extra_blocks=[block] if block is not None else [],
            memory_sink=build_mingchan_world_memory_sink(),
            display_content=display_content,
            media_asset_id=media_asset_id,
            media_asset_owner_id=media_asset_owner_id,
        )
    )


__all__ = [
    "read_companion_world_context",
    "run_mingchan_companion_world_turn",
]
