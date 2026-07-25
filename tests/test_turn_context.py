from app.agent_runtime.context.models import TurnContext
from app.products.zhaoxi.tools.registry import ZHAOXI_TOOL_POLICY


def test_turn_context_fields():
    ctx = TurnContext(
        account_id="acc-1",
        app_id="zhaoxi",
        tool_policy=ZHAOXI_TOOL_POLICY,
        account={"id": "acc-1"},
        session={"id": 1},
        identity=None,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="你好",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )
    assert ctx.account_id == "acc-1"
    assert ctx.app_id == "zhaoxi"
    assert ctx.binding["chat_id"] == "chat-1"
    assert ctx.recent_messages == []
    assert ctx.web_search_enabled is False
