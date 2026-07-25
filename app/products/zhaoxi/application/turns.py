"""朝夕对话入口；把朝夕产品服务显式注入通用 Agent Runtime。"""

from __future__ import annotations

from app.agent_runtime.turns.service import (
    ChannelTurnInput,
    build_product_channel_input_from_openclaw,
    build_product_turn_llm_input,
    handle_product_openclaw_turn,
    run_product_turn,
)
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.application.turn_services import ZHAOXI_TURN_SERVICES


def run_zhaoxi_turn(ctx: ChannelTurnInput):
    """执行一次显式绑定朝夕产品服务的 turn。"""

    return run_product_turn(ctx, product_services=ZHAOXI_TURN_SERVICES)


def build_zhaoxi_channel_input_from_openclaw(payload, **kwargs):
    """把 OpenClaw payload 规范化为朝夕 ChannelTurnInput。"""

    return build_product_channel_input_from_openclaw(
        payload,
        app_id=ZHAOXI_APP_ID,
        **kwargs,
    )


def handle_zhaoxi_openclaw_turn(payload, **kwargs):
    """执行一次朝夕 OpenClaw 入站 turn。"""

    return handle_product_openclaw_turn(
        payload,
        app_id=ZHAOXI_APP_ID,
        product_services=ZHAOXI_TURN_SERVICES,
        **kwargs,
    )


def build_zhaoxi_turn_llm_input(**kwargs):
    """构造朝夕兼容的 LLM 输入，供 debug 与既有调用方使用。"""

    return build_product_turn_llm_input(
        product_services=ZHAOXI_TURN_SERVICES,
        **kwargs,
    )


__all__ = [
    "build_zhaoxi_channel_input_from_openclaw",
    "build_zhaoxi_turn_llm_input",
    "handle_zhaoxi_openclaw_turn",
    "run_zhaoxi_turn",
]
