"""鸣蝉居民对话的 ProductTurnServices 实现。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from app.agent_runtime.persistence import profile_storage
from app.agent_runtime.turns.contracts import (
    AfterTurnHook,
    ProductPromptContext,
    ProductSessionSetup,
)
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db import get_or_create_session
from app.products.mingchan.tools.registry import MINGCHAN_TOOL_POLICY

_ONBOARDING_COMPLETE = "complete"
_PROFILE_FILENAMES = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")


def _business_day_for(now: datetime, *, start_hour: int) -> str:
    """按产品 session 日界线计算业务日。"""

    if start_hour < 0 or start_hour > 23:
        raise ValueError("start_hour must be between 0 and 23")
    day = now.date() - (timedelta(days=1) if now.hour < start_hour else timedelta())
    return day.isoformat()


class MingchanTurnServices:
    """鸣蝉居民 turn 策略；不继承或调用任何朝夕产品实现。"""

    app_id = MINGCHAN_APP_ID
    tool_policy = MINGCHAN_TOOL_POLICY
    onboarding_pending = "pending"
    onboarding_step1_sent = "step1_sent"
    onboarding_step2_sent = "step2_sent"
    onboarding_step3_sent = "step3_sent"
    onboarding_complete = _ONBOARDING_COMPLETE
    onboarding_welcome_text = ""

    def __init__(
        self,
        registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    ) -> None:
        """绑定可信产品注册表；生产禁用态与测试启用态不共享隐式全局。"""

        self.registry = registry
        self.allowed_channels = registry.require_registered(
            self.app_id
        ).allowed_channels

    def localized_message(
        self,
        key: str,
        *,
        fallback: Optional[str] = None,
        **params: Any,
    ) -> str:
        """返回鸣蝉固定文案；未建立文案表的通用错误沿用 Runtime fallback。"""

        message = str(fallback or key)
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
        """准备鸣蝉 App active session，不接入朝夕 Dreaming/onboarding。"""

        del memory_sink
        business_day = _business_day_for(now, start_hour=start_hour)
        state = get_or_create_session(
            account_id=account_id,
            channel=channel,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            session_key=active_session_key,
            business_day=business_day,
            update_account_channel=update_account_channel,
        )
        return ProductSessionSetup(
            business_day=business_day,
            state=state,
            profile_path=None,
        )

    def is_onboarding_active(self, state: str) -> bool:
        """居民人设已在 World 确认阶段完成，不进入朝夕聊天 onboarding。"""

        return False

    def get_onboarding_state(self, account_id: str) -> str:
        """鸣蝉居民 turn 恒按已完成人设初始化处理。"""

        del account_id
        return self.onboarding_complete

    def start_onboarding(self, account_id: str) -> None:
        """防止未来渠道能力误配后静默进入朝夕 onboarding。"""

        raise RuntimeError(f"mingchan onboarding is not supported: {account_id}")

    async def extract_onboarding_info(
        self, *, user_text: str, current_state: str
    ) -> dict:
        """鸣蝉居民不使用 turn 内 onboarding 提取。"""

        raise RuntimeError(f"mingchan onboarding is not supported: {current_state}")

    def apply_onboarding_info(
        self, *, account_id: str, extracted: dict, current_state: str
    ) -> dict:
        """鸣蝉居民不允许 turn 内改写人设。"""

        raise RuntimeError(f"mingchan onboarding is not supported: {account_id}")

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
        """从居民 runtime account 文件加载鸣蝉人设和记忆上下文。"""

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
            agent_context_metadata={
                "files": sorted(f"{key}.md" for key in blocks),
                "chars": {key: len(value) for key, value in blocks.items()},
            },
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
        """鸣蝉居民没有 turn 内 onboarding 状态机。"""

        raise RuntimeError(f"mingchan onboarding is not supported: {account_id}")

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        """当前居民 turn 不复用朝夕记忆或关系 hook。"""

        return ()


MINGCHAN_TURN_SERVICES = MingchanTurnServices()


__all__ = ["MINGCHAN_TURN_SERVICES", "MingchanTurnServices"]
