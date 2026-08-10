"""中性 test_product 对通用 Runtime 的最小真实调用契约。"""

from __future__ import annotations

from app.agent_runtime.turns.contracts import ProductPromptContext, ProductSessionSetup
from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn
from app.bootstrap.product_registry import build_test_product_registry
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.tools.registry import SHARED_TOOL_REGISTRY, ToolPolicy

import app.db as db


class NeutralProductTurnServices:
    """仅供 contract 测试的无业务产品服务，不创建生产产品目录。"""

    app_id = "test_product"
    allowed_channels = (CHANNEL_APP,)
    tool_policy = ToolPolicy.allow_all(
        app_id=app_id,
        catalog=SHARED_TOOL_REGISTRY,
    )
    # 中性产品没有引导流程：契约上直接用 None 表达，而不是实现一组抛异常的桩方法。
    onboarding = None

    def __init__(self) -> None:
        self.registry = build_test_product_registry()

    def prepare_session(self, **kwargs) -> ProductSessionSetup:
        state = db.get_or_create_session(
            account_id=kwargs["account_id"],
            channel=kwargs["channel"],
            sender_id=kwargs["sender_id"],
            sender_name=kwargs["sender_name"],
            chat_id=kwargs["chat_id"],
            session_key=kwargs["active_session_key"],
            business_day=kwargs["now"].date().isoformat(),
            update_account_channel=kwargs["update_account_channel"],
        )
        return ProductSessionSetup(
            business_day=kwargs["now"].date().isoformat(),
            state=state,
            profile_path=None,
        )

    def is_onboarding_active(self, state: str) -> bool:
        return False

    def get_onboarding_state(self, account_id: str) -> str:
        return self.onboarding_complete

    def start_onboarding(self, account_id: str) -> None:  # pragma: no cover
        raise AssertionError("中性产品不应进入 onboarding")

    async def extract_onboarding_info(self, **kwargs) -> dict:  # pragma: no cover
        raise AssertionError("中性产品不应提取 onboarding")

    def apply_onboarding_info(self, **kwargs) -> dict:  # pragma: no cover
        raise AssertionError("中性产品不应写 onboarding")

    def load_prompt_context(self, **kwargs) -> ProductPromptContext:
        return ProductPromptContext(
            soul="You are a neutral test agent.",
            user_prefs="",
            long_term_memory="",
            agent_context_blocks={},
            agent_context_metadata={},
            tool_flags={
                "web_search_enabled": False,
                "tdai_search_enabled": False,
            },
            tool_metadata={},
            tool_instructions=None,
            agent_self_state=None,
            onboarding_context="",
        )

    def advance_onboarding(self, **kwargs):  # pragma: no cover
        raise AssertionError("中性产品不应推进 onboarding")

    def after_turn_hooks(self):
        return ()


def test_neutral_product_executes_minimal_turn_without_zhaoxi_tools(
    fresh_db, monkeypatch
):
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037920")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="中性产品 Agent",
        app_id="test_product",
        registry=registry,
    )["account"]
    db.set_account_onboarding_state(account_id=account["id"], state="complete")

    captured = {}

    def fake_generate_reply_with_tools(**kwargs):
        captured["tool_names"] = {
            schema["function"]["name"] for schema in kwargs["tools"]
        }
        captured["context_app_id"] = kwargs["ctx"].app_id
        return "neutral reply", None

    monkeypatch.setattr("app.turn_service.settings", fresh_db)
    monkeypatch.setattr(
        "app.turn_service.generate_reply_with_tools", fake_generate_reply_with_tools
    )
    monkeypatch.setattr(
        "app.turn_service.record_chat_usage_charge", lambda **_: None
    )

    identity = ResolvedIdentity(
        ai4all_account_id=account["id"],
        session_key="app:test-product",
        channel=CHANNEL_APP,
        channel_account_id=user["id"],
        sender_id=user["id"],
        chat_id=None,
    )
    response = run_product_turn(
        ChannelTurnInput(
            account_id=account["id"],
            app_id="test_product",
            cap=CHANNELS[CHANNEL_APP],
            identity=identity,
            message_id="neutral-msg-1",
            event_id=None,
            message_type="text",
            text="hello",
            media=None,
            raw={"source": "test_product_contract"},
            sender_name="测试用户",
        ),
        product_services=NeutralProductTurnServices(),
    )

    assert response.status == "ok"
    assert response.reply == "neutral reply"
    assert captured["context_app_id"] == "test_product"
    assert {"web_fetch", "read"} <= captured["tool_names"]
    assert "create_reminder" not in captured["tool_names"]
    assert "mission_status" not in captured["tool_names"]
    assert [
        row["content"]
        for row in db.list_recent_messages_for_account(
            account_id=account["id"], limit=10
        )
    ] == ["hello", "neutral reply"]
