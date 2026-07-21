"""Agent Runtime 端口契约（ADR §7.3 四接缝）——M2-A 端口骨架，仅定形状、不接线。

方向恒 **域层 → Runtime**，Runtime 不反向依赖 Companion World（D-06）。本模块只声明冻结的
**接缝形状**（DTO 字段/类型 + Protocol 方法签名 + 事务归属），不含 adapter 实现、无 live
调用方；具体实现与接线随后续刀从真实调用点长出（D-02「方法从真实调用点长出、不提前落死」）。

四接缝（见 docs/tech_design/companion_world_3_0_refactor_design.md §7.3）：
  ① turn 外部 context 注入（入向）——复用现有 ContextBlock，经 ChannelTurnInput.extra_blocks
     携带（该字段的加性落点属 turn_service，M2-B）；本模块不重定义 ChannelTurnInput。
  ② after-turn typed memory sink（出向）——MemoryEvent + MemorySink.emit。
  ③ proactive delivery port（出向，修依赖反转）——ProactiveIntent + ProactiveDeliveryAdapter.deliver。
  ④ 事务边界（防孤儿账号）——UnitOfWork 共享单事务；money 路径留平台层、不进 Runtime 事务。

纯类型模块：`from __future__ import annotations` 使全部注解为字符串，外部类型仅在
TYPE_CHECKING 下引用，**零运行时 import**，不与 turn_service/prompt_builder 形成导入环。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

if TYPE_CHECKING:  # 仅供类型检查，运行时不导入（避免导入环）
    from app.db._backend import Connection
    from app.prompt_builder import ContextBlock  # noqa: F401  接缝① 复用类型
    from app.turn_service import ChannelTurnInput, OpenClawTurnResponse


# ── 接缝② after-turn typed memory sink（出向，typed；raw write_memory 不变）──────────
@dataclass(frozen=True)
class MemoryProvenance:
    """一条 typed memory 事件的来源溯源（provenance，永不丢）。"""

    source_account_id: str          # 事件来源 runtime account
    turn_message_id: Optional[str]
    session_id: Optional[int]
    business_day: str
    occurred_at: str                # ISO8601，由调用侧注入


@dataclass(frozen=True)
class MemoryEvent:
    """after-turn 产出的一条结构化事实事件（非逐字原文，D-05）。

    fact_type 路由（L2/L3）在**域层** sink 决定，Runtime 不认识 universe（D-06）；本模块只声明
    fact_type 为 str，枚举全集/路由矩阵属域层 + M2-B（见 P1 spec §1）。
    """

    fact_type: str
    payload: Dict[str, Any]         # 结构化事实，非逐字原文（原文仍 per-account，D-05）
    provenance: MemoryProvenance


class MemorySink(Protocol):
    """typed memory 汇（域层实现；Runtime hook 只 emit，不路由）。"""

    def emit(self, event: MemoryEvent) -> None: ...


# ── 接缝③ proactive delivery port（出向，修依赖反转）────────────────────────────────
@dataclass(frozen=True)
class ProactiveIntent:
    """「谁要主动对谁说什么」——不解析投递介质（介质由 adapter 决定）。"""

    platform_user_id: str           # 真人级键（人级去重/预算锚，与 D-09 同源）
    speaker_account_id: str         # 发声 resident 的 runtime account
    category: str                   # USER_REMINDER / NEW_USER_REACTIVATION / ...
    text: str
    idempotency_key: str
    universe_id: Optional[str] = None       # 形态 B 有；形态 A（微信）None
    resident_id: Optional[str] = None
    product_category: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """一次主动投递的结果。"""

    status: str                     # sent | enqueued | inbox | skipped
    channel: str                    # weixin | app_inbox
    ref_id: Optional[str]           # outbound_message.id 或 app_notification.id


class ProactiveDeliveryAdapter(Protocol):
    """按目标真人可达渠道投递 ProactiveIntent（WeixinAdapter / AppInboxAdapter 等）。"""

    def deliver(self, intent: ProactiveIntent) -> DeliveryResult: ...


# ── 接缝④ 事务边界（防孤儿账号；共享 UoW 单事务优先）─────────────────────────────
class UnitOfWork(Protocol):
    """单 PG 事务边界：同一 UoW 内多次 repo 写同事务提交/回滚。

    money 路径（钱包）留平台层、经 idempotency_key 跨事务对齐，**不进** Runtime 事务
    （ADR §7.3 ④、D-14）——故 create_resident+runtime 的 UoW 不持钱包锁（L4）。
    """

    conn: "Connection"

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, *exc: Any) -> None: ...


@dataclass(frozen=True)
class ResidentHandle:
    """create_runtime/create_resident_with_runtime 的返回句柄（字段随 M2-C 真实调用点firm）。"""

    resident_id: str
    runtime_account_id: str
    universe_id: str


# ── 接缝① 域层唯一入口：AgentRuntimePort（方向恒 域层→Runtime）─────────────────────
class AgentRuntimePort(Protocol):
    """域层访问 Agent Runtime 的唯一入口；Runtime 不反向依赖 World（D-06）。

    L3 等 shared-context 经 ChannelTurnInput.extra_blocks 携带（接缝①），Runtime 只消费、
    不认识 universe_id。P1 实际只需 send_turn / resolve_conversation_account
    （conversation_id→runtime_account_id）/ create_runtime(no-grant, M1-5) / set_read_only(offline)；
    后两者签名随各自真实调用点（M2-C/M4）落定，configure_persona / get_runtime_summary 无调用方
    暂不定义（D-02，避免提前锁死）。本骨架先落已冻结、签名清晰的两个方法。
    """

    def send_turn(self, ctx: "ChannelTurnInput") -> "OpenClawTurnResponse": ...

    def resolve_conversation_account(
        self, conversation_id: str, owner_platform_user_id: str
    ) -> str: ...
