"""Plum 对共享 Human-AI Runtime 的产品能力实现。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.agent_runtime.persistence import profile_storage
from app.agent_runtime.turns.contracts import (
    AfterTurnHook,
    ProductPromptContext,
    ProductSessionSetup,
)
from app.bootstrap.product_registry import (
    PLUM_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db import get_or_create_session
from app.products.plum.tools.registry import PLUM_TOOL_POLICY

_PROFILE_FILENAMES = ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")


class PlumTurnServices:
    """只投影 Plum 人设与会话规则，不依赖其他产品实现。"""

    app_id = PLUM_APP_ID
    tool_policy = PLUM_TOOL_POLICY
    # Plum 没有聊天内引导流程；None 让 Runtime 整条短路，取代此前那组 raise 桩实现。
    onboarding = None
    provisional_actor_owner_kinds = ("guest",)

    def __init__(self, registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY) -> None:
        self.registry = registry
        self.allowed_channels = registry.require_registered(self.app_id).allowed_channels

    def localized_message(
        self, key: str, *, fallback: Optional[str] = None, **params: Any
    ) -> str:
        messages = {
            "generation_error": "刚才有点走神了，请再说一次。",
            "rate_limit_rpm": "消息来得有点快，稍等一下再发给我。",
            "rate_limit_daily": "今天聊得有点多了，我们稍后继续。",
        }
        message = messages.get(key, str(fallback or key))
        try:
            return message.format(**params)
        except (KeyError, ValueError, IndexError):
            return message

    def prepare_session(
        self,
        *,
        account_id: str,
        channel: str,
        sender_id: str,
        sender_name: Optional[str],
        chat_id: Optional[str],
        now: datetime,
        start_hour: int,
        active_session_key: str,
        update_account_channel: bool,
        memory_sink: Any,
    ) -> ProductSessionSetup:
        del start_hour, memory_sink
        state = get_or_create_session(
            account_id=account_id,
            channel=channel,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            session_key=active_session_key,
            business_day=now.date().isoformat(),
            update_account_channel=update_account_channel,
        )
        return ProductSessionSetup(
            business_day=now.date().isoformat(),
            state=state,
            profile_path=None,
        )

    def load_prompt_context(
        self,
        *,
        account_id: str,
        account: Dict[str, Any],
        session: Dict[str, Any],
        channel: str,
        onboarding_state: str,
        onboarding_active: bool,
        onboarding_pre_written: Optional[Dict[str, Any]],
        onboarding_pre_extracted: Optional[Dict[str, Any]],
        include_tool_instructions: bool,
        now: datetime,
    ) -> ProductPromptContext:
        del (
            account,
            session,
            channel,
            onboarding_state,
            onboarding_active,
            onboarding_pre_written,
            onboarding_pre_extracted,
            include_tool_instructions,
            now,
        )
        blocks = {
            filename.removesuffix(".md"): content
            for filename in _PROFILE_FILENAMES
            if (content := profile_storage.read_file(account_id, filename))
        }
        return ProductPromptContext(
            soul=blocks.get("SOUL", ""),
            user_prefs=blocks.get("USER", ""),
            long_term_memory=blocks.get("MEMORY", ""),
            agent_context_blocks=blocks,
            agent_context_metadata={"files": sorted(f"{key}.md" for key in blocks)},
            tool_flags={},
            tool_metadata={},
            tool_instructions=None,
            agent_self_state=None,
            onboarding_context="",
        )

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        return ()


PLUM_TURN_SERVICES = PlumTurnServices()

__all__ = ["PLUM_TURN_SERVICES", "PlumTurnServices"]
