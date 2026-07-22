"""Companion World 领域契约：纯 DTO、稳定错误与 repository 端口。

本模块不依赖 ``app.db`` / ``app.turn_service``。SQL、事务连接与 profile 存储细节由
``app.platform.companion_world_repository`` 实现，领域 service 只编排状态与容量规则。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ContextManager, Optional, Protocol, Sequence, Tuple


class CompanionWorldError(Exception):
    """可稳定映射到 API 的领域错误；``code`` 不包含展示文案。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorldRecord:
    """一个真人的 home universe 状态。"""

    id: str
    owner_platform_user_id: str
    status: str
    onboarding_state: str
    legacy_primary_account_id: Optional[str] = None


@dataclass(frozen=True)
class TemplateRecord:
    """内部模板快照；``persona_seed_json`` 只供 runtime 实例化，禁止进入 App DTO。"""

    id: str
    source_type: str
    owner_platform_user_id: Optional[str]
    name: str
    avatar_ref: Optional[str]
    summary: Optional[str]
    tags: Tuple[str, ...]
    persona_seed_json: Optional[str]
    persona_version: str
    status: str
    initial_candidate_rank: Optional[int] = None


@dataclass(frozen=True)
class CandidateRecord:
    """world 内已钉住 template id/version 的候选关系。"""

    resident_id: str
    universe_id: str
    template: TemplateRecord
    template_version: str
    origin: str
    status: str
    runtime_account_id: Optional[str] = None
    conversation_id: Optional[str] = None


@dataclass(frozen=True)
class ResidentRecord:
    """一个已实例化（或历史 offline）的居民及其稳定 conversation。"""

    resident_id: str
    universe_id: str
    template_id: str
    template_version: str
    runtime_account_id: str
    origin: str
    status: str
    name: str
    avatar_ref: Optional[str]
    conversation_id: str
    conversation_state: str


@dataclass(frozen=True)
class ResidentSelection:
    """确认候选时的一项选择；display_name 为空则沿用模板名。"""

    template_id: str
    display_name: Optional[str] = None


@dataclass(frozen=True)
class TemplateDraft:
    """自建模板的内部输入；人设 JSON 由上层受控构造，领域层不解释其正文。"""

    name: str
    persona_seed_json: str
    avatar_ref: Optional[str] = None
    summary: Optional[str] = None
    tags: Tuple[str, ...] = ()
    persona_version: str = "v1"


@dataclass(frozen=True)
class ConversationTarget:
    """owner-scoped conversation 解析结果，供 history/turn 服务端定位 runtime。"""

    conversation_id: str
    universe_id: str
    resident_id: str
    owner_platform_user_id: str
    runtime_account_id: str
    state: str


@dataclass(frozen=True)
class ConversationSummary:
    """conversation 列表公开模型；P1 unread 恒为 0。"""

    conversation_id: str
    resident_id: str
    resident_name: str
    resident_avatar_ref: Optional[str]
    resident_status: str
    state: str
    last_preview: Optional[str]
    unread: int


@dataclass(frozen=True)
class ConversationMessage:
    """App 私聊历史中的一条用户可见消息。"""

    id: int
    message_id: Optional[str]
    role: str
    message_type: str
    content: str
    created_at: str


@dataclass(frozen=True)
class BootstrapResult:
    """幂等 bootstrap 的领域返回。"""

    world: WorldRecord
    candidates: Tuple[CandidateRecord, ...]


class WorldRepository(Protocol):
    """Companion World 的持久化端口；事务内返回同接口的绑定实例。"""

    def transaction(self) -> ContextManager["WorldRepository"]: ...

    def get_or_create_home_universe(self, platform_user_id: str) -> WorldRecord: ...

    def get_home_universe(self, platform_user_id: str) -> Optional[WorldRecord]: ...

    def lock_universe(self, universe_id: str) -> WorldRecord: ...

    def set_universe_onboarding_state(
        self, universe_id: str, onboarding_state: str
    ) -> WorldRecord: ...

    def list_initial_templates(self) -> Sequence[TemplateRecord]: ...

    def get_available_template(
        self, template_id: str, owner_platform_user_id: str
    ) -> Optional[TemplateRecord]: ...

    def get_template_for_owner(
        self, template_id: str, owner_platform_user_id: str
    ) -> Optional[TemplateRecord]: ...

    def create_custom_template(
        self, owner_platform_user_id: str, draft: TemplateDraft
    ) -> TemplateRecord: ...

    def ensure_candidate(
        self, universe_id: str, template: TemplateRecord, origin: str
    ) -> CandidateRecord: ...

    def list_candidates(
        self, universe_id: str, statuses: Sequence[str]
    ) -> Sequence[CandidateRecord]: ...

    def count_active_residents(self, universe_id: str) -> int: ...

    def activate_candidate_with_runtime(
        self,
        candidate: CandidateRecord,
        owner_platform_user_id: str,
        display_name: str,
    ) -> ResidentRecord: ...

    def dismiss_unselected_candidates(
        self, universe_id: str, selected_template_ids: Sequence[str]
    ) -> None: ...

    def list_residents_for_owner(
        self, platform_user_id: str, statuses: Sequence[str]
    ) -> Sequence[ResidentRecord]: ...

    def resolve_conversation_for_owner(
        self, conversation_id: str, platform_user_id: str
    ) -> Optional[ConversationTarget]: ...

    def list_conversations_for_owner(
        self,
        platform_user_id: str,
        cursor_conversation_id: Optional[str],
        limit: int,
    ) -> Sequence[ConversationSummary]: ...

    def list_conversation_messages(
        self,
        runtime_account_id: str,
        before_id: Optional[int],
        limit: int,
    ) -> Sequence[ConversationMessage]: ...

    # C2 backfill 使用的原语；仍遵循同一 transaction()/L1 锁序。
    def list_active_legacy_account_ids(self, platform_user_id: str) -> Sequence[str]: ...

    def ensure_legacy_resident(
        self, world: WorldRecord, runtime_account_id: str
    ) -> ResidentRecord: ...

    def mark_legacy_world(
        self, universe_id: str, legacy_primary_account_id: str
    ) -> WorldRecord: ...
