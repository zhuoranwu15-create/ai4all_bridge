"""鸣蝉 World 首次引导：bootstrap、候选确认与居民列表。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import ContextManager, Dict, Optional, Protocol, Sequence, Tuple

from app.products.mingchan.domain.companion_world.onboarding_content import (
    intro_content_for_persona,
)

MIN_INITIAL_CANDIDATES = 4
MAX_ACTIVE_RESIDENTS = 10
MAX_DISPLAY_NAME_CHARS = 20

_DISPLAY_NAME_ALLOWED_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7A3),
)
_DISPLAY_NAME_ALLOWED_ASCII = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. ·"
)
_NAME_SEPARATOR = "\x1f"


class MingchanWorldError(Exception):
    """可稳定映射到鸣蝉 API 的 World 领域错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WorldRecord:
    """鸣蝉用户的 home world 状态。"""

    id: str
    owner_platform_user_id: str
    status: str
    onboarding_state: str


@dataclass(frozen=True)
class TemplateRecord:
    """候选模板内部快照；persona_seed 不得进入 API DTO。"""

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
    persona_key: Optional[str] = None
    long_summary: Optional[str] = None
    name_pool: Tuple[str, ...] = ()
    name_pool_version: Optional[str] = None


@dataclass(frozen=True)
class CandidateRecord:
    """World 内已经钉住模板版本的候选居民。"""

    resident_id: str
    universe_id: str
    template: TemplateRecord
    template_version: str
    origin: str
    status: str
    runtime_account_id: Optional[str] = None
    conversation_id: Optional[str] = None
    suggested_display_name: Optional[str] = None
    naming_version: Optional[str] = None


@dataclass(frozen=True)
class ResidentRecord:
    """已经实例化并绑定鸣蝉 runtime account 的居民。"""

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
    persona_key: Optional[str] = None


@dataclass(frozen=True)
class ResidentSelection:
    """确认候选时的一项选择。"""

    template_id: str
    display_name: Optional[str] = None


@dataclass(frozen=True)
class BootstrapResult:
    """鸣蝉 bootstrap 返回；全新产品不会带入朝夕 legacy resident。"""

    world: WorldRecord
    candidates: Tuple[CandidateRecord, ...]
    residents: Tuple[ResidentRecord, ...] = ()


class MingchanWorldOnboardingRepository(Protocol):
    """鸣蝉 World onboarding 所需的最小 persistence 端口。"""

    def transaction(self) -> ContextManager["MingchanWorldOnboardingRepository"]: ...

    def get_or_create_home_universe(self, platform_user_id: str) -> WorldRecord: ...

    def get_home_universe(self, platform_user_id: str) -> Optional[WorldRecord]: ...

    def lock_universe(self, universe_id: str) -> WorldRecord: ...

    def set_universe_onboarding_state(
        self, universe_id: str, onboarding_state: str
    ) -> WorldRecord: ...

    def list_initial_templates(self) -> Sequence[TemplateRecord]: ...

    def ensure_candidate(
        self,
        universe_id: str,
        template: TemplateRecord,
        *,
        suggested_display_name: Optional[str],
        naming_version: Optional[str],
    ) -> CandidateRecord: ...

    def list_candidates(
        self, universe_id: str, statuses: Sequence[str]
    ) -> Sequence[CandidateRecord]: ...

    def count_active_residents(self, universe_id: str) -> int: ...

    def activate_candidate_with_runtime(
        self,
        candidate: CandidateRecord,
        *,
        owner_platform_user_id: str,
        display_name: str,
    ) -> ResidentRecord: ...

    def seed_resident_intro(
        self,
        resident: ResidentRecord,
        *,
        welcome_message: str,
        intro_post: str,
    ) -> None: ...

    def dismiss_unselected_candidates(
        self, universe_id: str, selected_template_ids: Sequence[str]
    ) -> None: ...

    def list_residents_for_owner(
        self, platform_user_id: str, statuses: Sequence[str]
    ) -> Sequence[ResidentRecord]: ...


def _is_valid_display_name(value: str) -> bool:
    text = (value or "").strip()
    if not text or len(text) > MAX_DISPLAY_NAME_CHARS:
        return False
    for char in text:
        if char in _DISPLAY_NAME_ALLOWED_ASCII:
            continue
        point = ord(char)
        if any(low <= point <= high for low, high in _DISPLAY_NAME_ALLOWED_RANGES):
            continue
        return False
    return True


def _select_suggested_name(
    *,
    universe_id: str,
    template_id: str,
    name_pool: Sequence[str],
    name_pool_version: str,
) -> Optional[str]:
    pool = tuple(name for name in name_pool if name)
    if not pool:
        return None
    digest = hashlib.sha256(
        _NAME_SEPARATOR.join(
            (universe_id, template_id, name_pool_version or "")
        ).encode("utf-8")
    ).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


class MingchanWorldOnboardingService:
    """编排鸣蝉全新 World；不查询或带入任何朝夕账号。"""

    def __init__(self, repository: MingchanWorldOnboardingRepository) -> None:
        self._repository = repository

    @staticmethod
    def _ensure_world_available(world: WorldRecord) -> None:
        if world.status != "active":
            raise MingchanWorldError("world_disabled")

    @staticmethod
    def _persona_seed_ready(raw: Optional[str]) -> bool:
        try:
            value = json.loads(raw or "")
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(value, dict):
            return False
        soul = value.get("SOUL.md", value.get("soul"))
        identity = value.get("IDENTITY.md", value.get("identity"))
        return all(isinstance(item, str) and item.strip() for item in (soul, identity))

    @classmethod
    def _validate_initial_catalog(
        cls, templates: Sequence[TemplateRecord]
    ) -> Tuple[TemplateRecord, ...]:
        ordered = tuple(
            sorted(templates, key=lambda item: item.initial_candidate_rank or 0)
        )
        ranks = tuple(item.initial_candidate_rank for item in ordered)
        metadata_ready = all(
            item.status == "active"
            and item.name.strip()
            and (item.avatar_ref or "").strip()
            and (item.summary or "").strip()
            and len(item.tags) == 3
            and cls._persona_seed_ready(item.persona_seed_json)
            and item.persona_version.strip()
            for item in ordered
        )
        if (
            len(ordered) < MIN_INITIAL_CANDIDATES
            or ranks != tuple(range(1, len(ordered) + 1))
            or not metadata_ready
        ):
            raise MingchanWorldError("preset_catalog_not_ready")
        return ordered

    def bootstrap_home(self, platform_user_id: str) -> BootstrapResult:
        """幂等创建鸣蝉 home world 和候选，不执行朝夕 legacy carry-in。"""

        with self._repository.transaction() as repo:
            world = repo.get_or_create_home_universe(platform_user_id)
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)
            if world.onboarding_state == "preparing":
                templates = self._validate_initial_catalog(repo.list_initial_templates())
                for template in templates:
                    suggested = _select_suggested_name(
                        universe_id=world.id,
                        template_id=template.id,
                        name_pool=template.name_pool,
                        name_pool_version=template.name_pool_version or "",
                    )
                    repo.ensure_candidate(
                        world.id,
                        template,
                        suggested_display_name=suggested,
                        naming_version=(
                            template.name_pool_version if suggested else None
                        ),
                    )
                world = repo.set_universe_onboarding_state(world.id, "selecting")
            candidates = tuple(repo.list_candidates(world.id, ("candidate",)))
            residents = tuple(
                repo.list_residents_for_owner(platform_user_id, ("active",))
            )
        return BootstrapResult(world=world, candidates=candidates, residents=residents)

    def list_candidates(self, platform_user_id: str) -> Tuple[CandidateRecord, ...]:
        """返回鸣蝉用户当前仍可选择的候选。"""

        with self._repository.transaction() as repo:
            world = repo.get_home_universe(platform_user_id)
            if world is None or world.onboarding_state == "preparing":
                raise MingchanWorldError("world_not_ready")
            self._ensure_world_available(world)
            return tuple(repo.list_candidates(world.id, ("candidate",)))

    def confirm_residents(
        self,
        platform_user_id: str,
        selections: Sequence[ResidentSelection],
    ) -> Tuple[ResidentRecord, ...]:
        """在一个事务内确认候选并创建固定归属鸣蝉的居民账号。"""

        normalized: Dict[str, ResidentSelection] = {}
        for selection in selections:
            template_id = selection.template_id.strip()
            if not template_id or template_id in normalized:
                raise MingchanWorldError("resident_selection_invalid")
            normalized[template_id] = selection

        with self._repository.transaction() as repo:
            world = repo.get_home_universe(platform_user_id)
            if world is None:
                raise MingchanWorldError("world_not_ready")
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)
            candidates = tuple(
                repo.list_candidates(world.id, ("candidate", "active", "dismissed"))
            )
            by_template = {item.template.id: item for item in candidates}
            chosen = []
            for template_id, selection in normalized.items():
                candidate = by_template.get(template_id)
                if candidate is None or candidate.status == "dismissed":
                    raise MingchanWorldError("resident_not_found")
                chosen.append((candidate, selection))

            to_activate = [item for item, _ in chosen if item.status == "candidate"]
            active_count = repo.count_active_residents(world.id)
            if active_count + len(to_activate) <= 0:
                raise MingchanWorldError("resident_capacity_empty")
            if active_count + len(to_activate) > MAX_ACTIVE_RESIDENTS:
                raise MingchanWorldError("resident_capacity_exceeded")

            for candidate, selection in chosen:
                if candidate.status == "active":
                    continue
                display_name = (
                    selection.display_name
                    or candidate.suggested_display_name
                    or candidate.template.name
                ).strip()
                if not display_name:
                    raise MingchanWorldError("resident_selection_invalid")
                if selection.display_name and not _is_valid_display_name(display_name):
                    raise MingchanWorldError("display_name_invalid")
                resident = repo.activate_candidate_with_runtime(
                    candidate,
                    owner_platform_user_id=platform_user_id,
                    display_name=display_name,
                )
                self._seed_resident_intro(
                    repo,
                    resident,
                    candidate.template.persona_key,
                )

            repo.dismiss_unselected_candidates(world.id, tuple(normalized))
            repo.set_universe_onboarding_state(world.id, "confirmed")
            return tuple(
                repo.list_residents_for_owner(platform_user_id, ("active",))
            )

    def list_residents(self, platform_user_id: str) -> Tuple[ResidentRecord, ...]:
        """返回 owner 自己的鸣蝉 active/offline 居民。"""

        with self._repository.transaction() as repo:
            world = repo.get_home_universe(platform_user_id)
            if world is None:
                raise MingchanWorldError("world_not_ready")
            self._ensure_world_available(world)
            return tuple(
                repo.list_residents_for_owner(
                    platform_user_id,
                    ("active", "offline"),
                )
            )

    @staticmethod
    def _seed_resident_intro(
        repo: MingchanWorldOnboardingRepository,
        resident: ResidentRecord,
        persona_key: Optional[str],
    ) -> None:
        """为已配置人设幂等写入欢迎消息和首条 Feed，不生成通用兜底。"""

        content = intro_content_for_persona(persona_key)
        if content is None:
            return
        repo.seed_resident_intro(
            resident,
            welcome_message=content.welcome_message,
            intro_post=content.intro_post,
        )


__all__ = [
    "BootstrapResult",
    "CandidateRecord",
    "MingchanWorldError",
    "MingchanWorldOnboardingRepository",
    "MingchanWorldOnboardingService",
    "ResidentRecord",
    "ResidentSelection",
    "TemplateRecord",
    "WorldRecord",
]
