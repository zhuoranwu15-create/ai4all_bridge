"""朝夕对话产品服务；由 composition root 显式注入 Agent Runtime。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.agent_runtime.turns.contracts import (
    AfterTurnHook,
    ProductAfterTurnContext,
    ProductPromptContext,
    ProductSessionSetup,
)
from app.bootstrap.product_registry import (
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
    ZHAOXI_APP_ID,
)
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    get_account_onboarding_state,
    get_active_content_invitation,
    set_account_onboarding_state,
)
from app.agent_runtime.context.prompt_builder import extract_section
from app.products.zhaoxi.application.memory.session_lifecycle import (
    business_day_for,
    get_or_create_account_active_session_with_dreaming,
)
from app.products.zhaoxi.application.memory.writer import write_memory
from app.products.zhaoxi.application.missions.assignment import assign_mission_if_absent
from app.products.zhaoxi.application.missions.self_state import build_agent_self_state_block
from app.products.zhaoxi.application.missions.state import resolve_account_mission
from app.products.zhaoxi.application.onboarding import (
    ONBOARDING_COMPLETE,
    ONBOARDING_PENDING,
    ONBOARDING_STEP1_SENT,
    ONBOARDING_STEP2_SENT,
    ONBOARDING_STEP3_SENT,
    ONBOARDING_WELCOME_TEXT,
    apply_extracted_onboarding_info,
    build_onboarding_prompt_context,
    extract_onboarding_info_async,
    is_onboarding_active,
    next_onboarding_state,
)
from app.products.zhaoxi.application.product_localization import (
    product_default_language,
    product_message,
)
from app.products.zhaoxi.application.onboarding_overrides import (
    resolve_onboarding_identity_overrides,
)
from app.products.zhaoxi.application.relationship import maybe_update_relationship_state_after_turn
from app.products.zhaoxi.infrastructure.profiles import (
    ensure_agent_context_files,
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)
from app.products.zhaoxi.proactive.store.account_state import ensure_account_state
from app.products.zhaoxi.tools.registry import ZHAOXI_TOOL_POLICY

logger = logging.getLogger("ai4all.products.zhaoxi.turn_services")


def _context_value(blocks: Dict[str, str], section: str, label: str) -> Optional[str]:
    """从朝夕 context block 中读取一个单行标签值。"""

    import re

    match = re.search(rf"{re.escape(label)}[:：]\s*(.+)", blocks.get(section, ""))
    return match.group(1).strip() if match else None


def _write_memory_after_turn(atx: ProductAfterTurnContext):
    return write_memory(
        account_id=atx.account_id,
        turns=[
            {"role": "user", "content": atx.text},
            {"role": "assistant", "content": atx.reply},
        ],
        today=atx.business_day,
        session_id=int(atx.session["id"]),
        user_message_id=atx.message_id,
        assistant_message_id=atx.reply_message_id,
        modality=atx.message_type,
        extra_metadata={
            "channel": atx.identity.channel,
            "channel_binding_id": atx.binding["id"],
            "openclaw_session_key": atx.openclaw_session_key,
            "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        },
    )


def _update_relationship_after_turn(atx: ProductAfterTurnContext):
    return asyncio.to_thread(
        maybe_update_relationship_state_after_turn,
        account_id=atx.account_id,
    )


class ZhaoxiTurnServices:
    """朝夕现有 turn 业务规则的显式 ProductTurnServices 实现。"""

    app_id = ZHAOXI_APP_ID
    tool_policy = ZHAOXI_TOOL_POLICY
    onboarding_pending = ONBOARDING_PENDING
    onboarding_step1_sent = ONBOARDING_STEP1_SENT
    onboarding_step2_sent = ONBOARDING_STEP2_SENT
    onboarding_step3_sent = ONBOARDING_STEP3_SENT
    onboarding_complete = ONBOARDING_COMPLETE
    onboarding_welcome_text = product_message("onboarding_welcome")

    def __init__(
        self,
        registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    ) -> None:
        """绑定可信产品注册表；Runtime 与计费共用同一产品启停上下文。"""

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
        """返回朝夕默认语言文案，并安全插值命名参数。"""

        language = product_default_language(self.app_id)
        if fallback and language == "zh-CN":
            message = str(fallback)
            try:
                return message.format(**params)
            except (KeyError, ValueError, IndexError):
                return message
        return product_message(key, app_id=self.app_id, language=language, **params)

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
        """准备朝夕 active session、profile 文件和主动状态。"""

        business_day = business_day_for(now, start_hour=start_hour)
        state = get_or_create_account_active_session_with_dreaming(
            account_id=account_id,
            channel=channel,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=chat_id,
            business_day=business_day,
            active_session_key=active_session_key,
            update_account_channel=update_account_channel,
            memory_sink=memory_sink,
        )
        account = state["account"]
        profile_path = ensure_user_profile(account_id)
        ensure_agent_context_files(
            account_id,
            display_name=account.get("display_name"),
            channel=channel,
        )
        ensure_account_state(account_id=account_id)
        return ProductSessionSetup(
            business_day=business_day,
            state=state,
            profile_path=profile_path,
        )

    def is_onboarding_active(self, state: str) -> bool:
        """判断朝夕 onboarding 是否仍在进行。"""

        return is_onboarding_active(state)

    def get_onboarding_state(self, account_id: str) -> str:
        """读取朝夕账号 onboarding 状态。"""

        return get_account_onboarding_state(account_id=account_id)

    def start_onboarding(self, account_id: str) -> None:
        """把朝夕 onboarding 标记为已发送第一步。"""

        set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)

    async def extract_onboarding_info(
        self, *, user_text: str, current_state: str
    ) -> dict:
        """调用朝夕 onboarding 提取器。"""

        return await extract_onboarding_info_async(
            user_text=user_text,
            current_state=current_state,
        )

    def apply_onboarding_info(
        self, *, account_id: str, extracted: dict, current_state: str
    ) -> dict:
        """按朝夕活码预设规则写入 onboarding 信息。"""

        overrides = resolve_onboarding_identity_overrides(account_id)
        return apply_extracted_onboarding_info(
            account_id=account_id,
            extracted=extracted,
            current_state=current_state,
            has_forced_soul_preset=overrides.forced_personality,
            has_forced_ai_name=overrides.forced_ai_name,
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
        """加载朝夕 profile、使命、内容邀请和 onboarding prompt 投影。"""

        file_profile = read_user_profile(account_id)
        agent_context = read_agent_context(
            account_id,
            display_name=account.get("display_name"),
            channel=channel,
        )
        active_invitation = None
        if not onboarding_active and include_tool_instructions:
            active_invitation = get_active_content_invitation(
                account_id=account_id,
                now=now.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
            )
        mission = None if onboarding_active else resolve_account_mission(account_id=account_id)
        self_state = (
            None
            if onboarding_active
            else build_agent_self_state_block(account_id=account_id, resolved_mission=mission)
        )
        onboarding_context = ""
        if onboarding_active:
            overrides = resolve_onboarding_identity_overrides(account_id)
            onboarding_context = build_onboarding_prompt_context(
                state=onboarding_state,
                user_name=_context_value(agent_context.blocks, "USER", "用户称呼"),
                ai_name=_context_value(agent_context.blocks, "IDENTITY", "AI 名字"),
                persona=(onboarding_pre_written or {}).get("persona"),
                user_name_ask_count=(
                    1
                    if onboarding_state
                    in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT, ONBOARDING_COMPLETE}
                    else 0
                ),
                persona_ask_count=0 if onboarding_state != ONBOARDING_STEP3_SENT else 1,
                needs_confirmation=bool(
                    (onboarding_pre_extracted or {}).get("needs_confirmation")
                ),
                onboarding_script_override=overrides.script_override,
                creator_opening_line=overrides.creator_opening_line,
                has_forced_soul_preset=overrides.forced_personality,
                has_forced_ai_name=overrides.forced_ai_name,
            )
        tool_instructions = None
        if active_invitation is not None:
            title_count = len(active_invitation.get("title_items") or [])
            tool_instructions = "\n".join(
                [
                    "## 当前内容邀请",
                    "",
                    (
                        f"- 当前存在待回应内容邀请：id={active_invitation['id']}，"
                        f"topic={active_invitation['topic']}，标题数={title_count}。"
                    ),
                    (
                        "- 本轮提供内容邀请回复工具：send_content_invitation_titles、"
                        "record_content_invitation_feedback。"
                    ),
                ]
            )
        return ProductPromptContext(
            soul=extract_section(file_profile, "Soul"),
            user_prefs=extract_section(file_profile, "User Preferences"),
            long_term_memory=extract_section(file_profile, "Long-term Memory"),
            agent_context_blocks=agent_context.blocks,
            agent_context_metadata=agent_context.metadata(),
            tool_flags={
                "content_invitation_response_enabled": active_invitation is not None,
                "has_mission": mission is not None,
            },
            tool_metadata={
                "active_content_invitation_id": (
                    active_invitation["id"] if active_invitation is not None else None
                ),
                "has_mission": mission is not None,
            },
            tool_instructions=tool_instructions,
            agent_self_state=self_state,
            onboarding_context=onboarding_context,
        )

    def advance_onboarding(
        self,
        *,
        account_id: str,
        current_state: str,
        extracted: Optional[dict],
        session_turn_count: int,
    ) -> Optional[str]:
        """推进朝夕 onboarding，并在完成时幂等分配使命。"""

        if current_state == ONBOARDING_PENDING:
            set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
            return ONBOARDING_STEP1_SENT
        if extracted is None:
            return None
        confirmation_ask_count = (
            max(0, int(session_turn_count) - 2)
            if current_state == ONBOARDING_STEP2_SENT
            else 0
        )
        overrides = resolve_onboarding_identity_overrides(account_id)
        new_state = next_onboarding_state(
            current_state=current_state,
            extracted=extracted,
            user_name_ask_count=0,
            persona_ask_count=0,
            confirmation_ask_count=confirmation_ask_count,
            has_forced_soul_preset=overrides.forced_personality,
            has_forced_ai_name=overrides.forced_ai_name,
        )
        if new_state == current_state:
            return None
        set_account_onboarding_state(account_id=account_id, state=new_state)
        if new_state == ONBOARDING_COMPLETE:
            try:
                assign_mission_if_absent(account_id=account_id)
            except Exception as err:
                logger.error("mission assignment failed account=%s error=%s", account_id, err)
        return new_state

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        """返回朝夕记忆和关系状态 hooks。"""

        return (
            ("write_memory", _write_memory_after_turn),
            ("relationship_state", _update_relationship_after_turn),
        )


ZHAOXI_TURN_SERVICES = ZhaoxiTurnServices()

__all__ = ["ZHAOXI_TURN_SERVICES", "ZhaoxiTurnServices"]
