"""产品 turn 服务端口；Runtime 通过这些端口消费产品差异。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Protocol, Tuple

from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import ChannelCapability

if TYPE_CHECKING:
    from app.bootstrap.product_registry import ProductRegistry
    from app.tools.registry import ToolPolicy


@dataclass(frozen=True)
class ProductSessionSetup:
    """产品 session 准备结果。"""

    business_day: str
    state: Dict[str, Any]
    profile_path: Optional[Path]


@dataclass(frozen=True)
class ProductPromptContext:
    """产品提供给通用 prompt 组装器的中性上下文投影。"""

    soul: str
    user_prefs: str
    long_term_memory: str
    agent_context_blocks: Dict[str, str]
    agent_context_metadata: Dict[str, Any]
    tool_flags: Dict[str, bool]
    tool_metadata: Dict[str, Any]
    tool_instructions: Optional[str]
    agent_self_state: Optional[str]
    onboarding_context: str


@dataclass(frozen=True)
class ProductAfterTurnContext:
    """产品 after-turn hook 可读取的最小、中性 turn 快照。"""

    account_id: str
    app_id: str
    text: str
    reply: str
    business_day: str
    session: Dict[str, Any]
    message_id: Optional[str]
    reply_message_id: str
    message_type: str
    identity: ResolvedIdentity
    binding: Dict[str, Any]
    openclaw_session_key: str
    normal_reply_generated: bool
    cap: ChannelCapability
    moderation_blocked: bool
    onboarding_active: bool
    image_understanding_failed: bool


AfterTurnHook = Callable[[ProductAfterTurnContext], Optional[Awaitable[Any]]]


class ProductOnboarding(Protocol):
    """产品可选的「首轮引导」能力。

    只有真正带 onboarding 状态机的产品（当前仅朝夕）实现本端口。没有引导流程的产品把
    ``ProductTurnServices.onboarding`` 置为 ``None``，Runtime 整条短路。此前这 6 个方法
    直接长在 ``ProductTurnServices`` 上，鸣蝉/Plum 只能编一组用不上的状态常量、再用
    ``raise RuntimeError`` 桩住实现——「本产品没有引导」只能靠运行时异常表达，
    等于把朝夕的流程泄漏进了通用 turn 契约。
    """

    # onboarding 状态机取值；由产品自行定义，Runtime 只做相等比较，不解释语义。
    pending: str
    step1_sent: str
    step2_sent: str
    step3_sent: str
    complete: str
    welcome_text: str

    def is_active(self, state: str) -> bool:
        """返回给定状态是否仍在引导过程中。"""

    def get_state(self, account_id: str) -> str:
        """读取账号在当前产品内的引导状态。"""

    def start(self, account_id: str) -> None:
        """记录首个欢迎步骤已发送。"""

    async def extract_info(self, *, user_text: str, current_state: str) -> dict:
        """从用户本轮输入提取引导信息。"""

    def apply_info(
        self, *, account_id: str, extracted: dict, current_state: str
    ) -> dict:
        """按产品规则写入已提取的引导信息。"""

    def advance(
        self,
        *,
        account_id: str,
        current_state: str,
        extracted: Optional[dict],
        session_turn_count: int,
    ) -> Optional[str]:
        """推进引导状态，返回推进后的状态；无变化返回 None。"""


class ProductTurnServices(Protocol):
    """Runtime 所需的最小产品能力；实现由产品 manifest 显式注入。"""

    app_id: str
    registry: "ProductRegistry"
    allowed_channels: Tuple[str, ...]
    tool_policy: "ToolPolicy"
    # None = 该产品没有首轮引导；Runtime 不做任何 onboarding 回调。
    onboarding: Optional[ProductOnboarding]
    provisional_actor_owner_kinds: Tuple[str, ...]

    def localized_message(
        self,
        key: str,
        *,
        fallback: Optional[str] = None,
        **params: Any,
    ) -> str:
        """返回产品语言下的固定用户文案，并支持命名参数插值。"""

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
        """准备产品 session、profile 和必要的账号级产品状态。"""

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
        """加载产品 profile、persona、mission 与 onboarding 的中性 prompt 投影。

        无 onboarding 的产品收到的是中性默认值（``onboarding_state=""``、
        ``onboarding_active=False``、两个 pre_* 为 None）。
        """

    def after_turn_hooks(self) -> Tuple[Tuple[str, AfterTurnHook], ...]:
        """返回产品拥有的 after-turn hooks，顺序稳定。"""
