"""Nooki 对话产品服务；由 composition root 显式注入 Agent Runtime。

Nooki 没有聊天内 onboarding 状态机（人设/起名走确定性 API，不占用聊天轮次），也没有
after-turn 副作用（任务写入全部发生在工具执行阶段，不需要额外 hook）——两点都和朝夕
的实现明显不同，其余结构照抄 `app.products.zhaoxi.application.turn_services`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.agent_runtime.turns.contracts import (
    AfterTurnHook,
    ProductPromptContext,
    ProductSessionSetup,
)
from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import get_or_create_session, get_platform_user_id_for_account
from app.products.nooki.application.persona import (
    DEFAULT_ARCHETYPE,
    DEFAULT_COMPANION_NAME,
    build_soul,
)
from app.products.nooki.application.tool_instructions import NOOKI_TOOL_INSTRUCTIONS
from app.products.nooki.infrastructure.repositories.user_profile import get_explicit_preferences
from app.products.nooki.tools.registry import NOOKI_TOOL_POLICY

ONBOARDING_PENDING = "pending"
ONBOARDING_STEP1_SENT = "step1_sent"
ONBOARDING_STEP2_SENT = "step2_sent"
ONBOARDING_STEP3_SENT = "step3_sent"
ONBOARDING_COMPLETE = "complete"


class NookiTurnServices:
    """Nooki 单任务闭环的 ProductTurnServices 实现：onboarding 中性，任务状态全部来自领域投影。"""

    app_id = NOOKI_APP_ID
    tool_policy = NOOKI_TOOL_POLICY
    onboarding_pending = ONBOARDING_PENDING
    onboarding_step1_sent = ONBOARDING_STEP1_SENT
    onboarding_step2_sent = ONBOARDING_STEP2_SENT
    onboarding_step3_sent = ONBOARDING_STEP3_SENT
    onboarding_complete = ONBOARDING_COMPLETE
    onboarding_welcome_text = ""

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
        """Nooki 用通用 session（无 dreaming/mission），profile 文件不适用（返回 None）。"""

        business_day = now.date().isoformat()
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
        return ProductSessionSetup(business_day=business_day, state=state, profile_path=None)

    def is_onboarding_active(self, state: str) -> bool:
        """Nooki 没有聊天内 onboarding 状态机，人设/起名走确定性 API。"""

        return False

    def get_onboarding_state(self, account_id: str) -> str:
        return self.onboarding_complete

    def start_onboarding(self, account_id: str) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("Nooki 不使用聊天内 onboarding 状态机")

    async def extract_onboarding_info(  # pragma: no cover - 不应被调用
        self, *, user_text: str, current_state: str
    ) -> dict:
        raise AssertionError("Nooki 不使用聊天内 onboarding 状态机")

    def apply_onboarding_info(  # pragma: no cover - 不应被调用
        self, *, account_id: str, extracted: dict, current_state: str
    ) -> dict:
        raise AssertionError("Nooki 不使用聊天内 onboarding 状态机")

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
        """人设（archetype+起名）+ 工具调用规则，全部来自 DB，不落文件。

        `account` 是 `accounts` 表原始行，不带 `platform_user_id`；这里显式用 account_id 反解。
        当前聚焦任务投影不走这里：`agent_context_blocks` 只有 PromptBuilder 白名单内的
        SOUL/IDENTITY/USER/MEMORY/MISSION 等固定 key 会被渲染，任意其它 key 会被静默丢弃。
        FOCUS_TASK 作为域层块改由 `turns.run_nooki_turn` 经 `ChannelTurnInput.extra_blocks`
        注入（ADR §7.3 接缝①，App 入口专用的通用机制，无需扩 Runtime 白名单）。
        """

        platform_user_id = get_platform_user_id_for_account(account_id=account_id)
        preferences = get_explicit_preferences(platform_user_id) if platform_user_id else {}
        archetype = preferences.get("archetype") or DEFAULT_ARCHETYPE
        companion_name = preferences.get("companion_name") or DEFAULT_COMPANION_NAME
        soul = build_soul(archetype=archetype, companion_name=companion_name)

        return ProductPromptContext(
            soul=soul,
            user_prefs="",
            long_term_memory="",
            agent_context_blocks={},
            agent_context_metadata={},
            tool_flags={},
            tool_metadata={},
            tool_instructions=NOOKI_TOOL_INSTRUCTIONS if include_tool_instructions else None,
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
        """Nooki 不推进聊天内 onboarding 状态。"""

        return None

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        """P0+P1 无 after-turn 副作用：任务读写全部发生在工具执行阶段。"""

        return ()


NOOKI_TURN_SERVICES = NookiTurnServices()

__all__ = ["NOOKI_TURN_SERVICES", "NookiTurnServices"]
