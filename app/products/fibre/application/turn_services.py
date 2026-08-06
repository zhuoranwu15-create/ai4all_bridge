"""Fibre 对共享 Human-AI Runtime 的产品能力实现。"""
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
    FIBRE_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db import get_or_create_session
from app.products.fibre.tools.registry import FIBRE_TOOL_POLICY

_PROFILE_FILENAMES = ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")


class FibreTurnServices:
    """只投影 Fibre 人设与会话规则，不依赖其他产品实现。"""

    app_id = FIBRE_APP_ID
    tool_policy = FIBRE_TOOL_POLICY
    onboarding_pending = "pending"
    onboarding_step1_sent = "step1_sent"
    onboarding_step2_sent = "step2_sent"
    onboarding_step3_sent = "step3_sent"
    onboarding_complete = "complete"
    onboarding_welcome_text = ""

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

    def is_onboarding_active(self, state: str) -> bool:
        del state
        return False

    def get_onboarding_state(self, account_id: str) -> str:
        del account_id
        return self.onboarding_complete

    def start_onboarding(self, account_id: str) -> None:
        raise RuntimeError(f"fibre onboarding is not supported: {account_id}")

    async def extract_onboarding_info(self, *, user_text: str, current_state: str) -> dict:
        raise RuntimeError(f"fibre onboarding is not supported: {current_state}")

    def apply_onboarding_info(
        self, *, account_id: str, extracted: dict, current_state: str
    ) -> dict:
        raise RuntimeError(f"fibre onboarding is not supported: {account_id}")

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

    def advance_onboarding(
        self,
        *,
        account_id: str,
        current_state: str,
        extracted: Optional[dict],
        session_turn_count: int,
    ) -> Optional[str]:
        raise RuntimeError(f"fibre onboarding is not supported: {account_id}")

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        return ()


FIBRE_TURN_SERVICES = FibreTurnServices()

__all__ = ["FIBRE_TURN_SERVICES", "FibreTurnServices"]
