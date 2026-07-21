"""Companion World 纯领域编排（bootstrap / confirm / create / owner resolve）。"""
from __future__ import annotations

import json
from typing import Dict, Optional, Sequence, Tuple

from app.domains.companion_world.contracts import (
    BootstrapResult,
    CandidateRecord,
    CompanionWorldError,
    ConversationTarget,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
    TemplateRecord,
    WorldRecord,
    WorldRepository,
)

MAX_ACTIVE_RESIDENTS = 10
INITIAL_CANDIDATE_RANKS = (1, 2, 3, 4)


class CompanionWorldService:
    """按冻结规则编排 world；所有写操作都经 repository 单事务执行。"""

    def __init__(self, repository: WorldRepository) -> None:
        self._repository = repository

    @staticmethod
    def _ensure_world_available(world: WorldRecord) -> None:
        if world.status != "active":
            raise CompanionWorldError("world_disabled")

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
        cls, templates: Sequence[TemplateRecord],
    ) -> Tuple[TemplateRecord, ...]:
        ordered = tuple(sorted(templates, key=lambda item: item.initial_candidate_rank or 0))
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
        if ranks != INITIAL_CANDIDATE_RANKS or not metadata_ready:
            raise CompanionWorldError("preset_catalog_not_ready")
        return ordered

    def bootstrap_home(self, platform_user_id: str) -> BootstrapResult:
        """幂等建立 home world，并仅在首次 preparing 时快照固定四位初始候选。"""
        with self._repository.transaction() as repo:
            world = repo.get_or_create_home_universe(platform_user_id)
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)
            if world.onboarding_state == "preparing":
                templates = self._validate_initial_catalog(repo.list_initial_templates())
                for template in templates:
                    repo.ensure_candidate(world.id, template, "preset")
                world = repo.set_universe_onboarding_state(world.id, "selecting")
            candidates = tuple(repo.list_candidates(world.id, ("candidate",)))
        return BootstrapResult(world=world, candidates=candidates)

    def confirm_residents(
        self,
        platform_user_id: str,
        selections: Sequence[ResidentSelection],
    ) -> Tuple[ResidentRecord, ...]:
        """确认 1–10 位本世界候选；建号、激活与 conversation 在同一事务完成。"""
        normalized: Dict[str, ResidentSelection] = {}
        for selection in selections:
            template_id = selection.template_id.strip()
            if not template_id or template_id in normalized:
                raise CompanionWorldError("resident_selection_invalid")
            normalized[template_id] = selection
        if not normalized:
            raise CompanionWorldError("resident_capacity_empty")

        with self._repository.transaction() as repo:
            world = repo.get_home_universe(platform_user_id)
            if world is None:
                raise CompanionWorldError("world_not_ready")
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
                    raise CompanionWorldError("resident_not_found")
                chosen.append((candidate, selection))

            to_activate = [item for item, _selection in chosen if item.status == "candidate"]
            if repo.count_active_residents(world.id) + len(to_activate) > MAX_ACTIVE_RESIDENTS:
                raise CompanionWorldError("resident_capacity_exceeded")

            for candidate, selection in chosen:
                if candidate.status == "active":
                    continue
                display_name = (selection.display_name or candidate.template.name).strip()
                if not display_name:
                    raise CompanionWorldError("resident_selection_invalid")
                repo.activate_candidate_with_runtime(
                    candidate,
                    owner_platform_user_id=platform_user_id,
                    display_name=display_name,
                )
            repo.dismiss_unselected_candidates(world.id, tuple(normalized))
            repo.set_universe_onboarding_state(world.id, "confirmed")
            residents = tuple(repo.list_residents_for_owner(platform_user_id, ("active",)))
        return residents

    def list_residents(
        self, platform_user_id: str
    ) -> Tuple[ResidentRecord, ...]:
        """返回 owner 自己世界的 active/offline 居民；无 world 返回 world_not_ready。"""
        world = self._repository.get_home_universe(platform_user_id)
        if world is None:
            raise CompanionWorldError("world_not_ready")
        self._ensure_world_available(world)
        return tuple(
            self._repository.list_residents_for_owner(
                platform_user_id, ("active", "offline")
            )
        )

    def list_candidates(
        self, platform_user_id: str
    ) -> Tuple[CandidateRecord, ...]:
        """返回 owner 当前仍可选择的快照候选；preparing world 要求先 bootstrap。"""
        world = self._repository.get_home_universe(platform_user_id)
        if world is None or world.onboarding_state == "preparing":
            raise CompanionWorldError("world_not_ready")
        self._ensure_world_available(world)
        return tuple(self._repository.list_candidates(world.id, ("candidate",)))

    def create_resident(
        self,
        platform_user_id: str,
        *,
        template_id: Optional[str] = None,
        custom_template: Optional[TemplateDraft] = None,
    ) -> CandidateRecord | ResidentRecord:
        """新增模板居民；selecting 期自建只落 candidate，confirmed 期直接原子激活。"""
        if bool(template_id) == bool(custom_template):
            raise CompanionWorldError("resident_selection_invalid")
        with self._repository.transaction() as repo:
            world = repo.get_home_universe(platform_user_id)
            if world is None:
                raise CompanionWorldError("world_not_ready")
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)

            if custom_template is not None:
                if (
                    not custom_template.name.strip()
                    or not custom_template.persona_version.strip()
                    or not self._persona_seed_ready(custom_template.persona_seed_json)
                ):
                    raise CompanionWorldError("resident_selection_invalid")
                existing_custom = [
                    item
                    for item in repo.list_candidates(
                        world.id, ("candidate", "active", "dismissed")
                    )
                    if item.origin == "custom"
                ]
                if world.onboarding_state == "selecting" and existing_custom:
                    raise CompanionWorldError("custom_candidate_limit_exceeded")
                template = repo.create_custom_template(platform_user_id, custom_template)
                origin = "custom"
            else:
                template = repo.get_template_for_owner(
                    template_id or "", platform_user_id
                )
                if template is None:
                    raise CompanionWorldError("template_not_found")
                if template.status != "active":
                    raise CompanionWorldError("template_not_available")
                origin = "preset"

            candidate = repo.ensure_candidate(world.id, template, origin)
            if candidate.status == "active":
                raise CompanionWorldError("resident_already_exists")
            if candidate.status != "candidate":
                raise CompanionWorldError("template_not_available")
            if world.onboarding_state == "selecting":
                return candidate
            if world.onboarding_state != "confirmed":
                raise CompanionWorldError("world_not_ready")
            if repo.count_active_residents(world.id) >= MAX_ACTIVE_RESIDENTS:
                raise CompanionWorldError("resident_capacity_exceeded")
            return repo.activate_candidate_with_runtime(
                candidate,
                owner_platform_user_id=platform_user_id,
                display_name=template.name,
            )

    def resolve_conversation(
        self, platform_user_id: str, conversation_id: str
    ) -> ConversationTarget:
        """owner-scoped 解析 conversation；不存在与越权统一 conversation_not_found。"""
        target = self._repository.resolve_conversation_for_owner(
            conversation_id, platform_user_id
        )
        if target is None:
            raise CompanionWorldError("conversation_not_found")
        return target
