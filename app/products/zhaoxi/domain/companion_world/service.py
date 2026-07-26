"""Companion World 纯领域编排（bootstrap / confirm / create / owner resolve）。"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Sequence, Tuple

from app.products.zhaoxi.domain.companion_world.contracts import (
    BootstrapResult,
    CandidateRecord,
    CompanionWorldError,
    ConversationMessage,
    ConversationSummary,
    ConversationTarget,
    ResidentDraftRecord,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
    TemplateRecord,
    WorldRecord,
    WorldRepository,
)
from app.products.zhaoxi.domain.companion_world.persona_catalog import (
    PERSONALITY_TRAITS,
    PersonaCatalogError,
    PersonaInput,
    RenderedPersona,
    is_valid_display_name,
    render_persona,
    resolve_avatar_ref,
)

logger = logging.getLogger(__name__)

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
        """幂等建立 home world；首次 preparing 时带入微信既有角色并快照四位初始候选。

        D-A（2026-07-26）：微信老用户在 App 侧**按新用户对待**——同样进入选择角色页、
        同样拿到四位候选、可叉掉不喜欢的、可自建，唯一区别是其微信侧既有角色被带入本世界
        （已 active，不作为候选，不可被叉掉）。因此这里不再把老用户直接推进 confirmed。
        """
        with self._repository.transaction() as repo:
            world = repo.get_or_create_home_universe(platform_user_id)
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)
            if world.onboarding_state == "preparing":
                world = self._carry_in_legacy_resident(repo, world, platform_user_id)
                templates = self._validate_initial_catalog(repo.list_initial_templates())
                for template in templates:
                    repo.ensure_candidate(world.id, template, "preset")
                world = repo.set_universe_onboarding_state(world.id, "selecting")
            candidates = tuple(repo.list_candidates(world.id, ("candidate",)))
            residents = tuple(repo.list_residents_for_owner(platform_user_id, ("active",)))
        return BootstrapResult(world=world, candidates=candidates, residents=residents)

    @staticmethod
    def _carry_in_legacy_resident(repo, world: WorldRecord, platform_user_id: str) -> WorldRecord:
        """把真人在微信侧的既有账号带入本世界，作为 active legacy 居民。

        产品口径保证一个真人在朝夕只有一个 active 账号，所以只带入最早绑定的那一个；
        出现多个视为异常数据，记录告警后仍只带第一个，不静默全量带入。
        只落 legacy primary 锚（微信主动消息路由的依据），不改 onboarding_state。
        """
        legacy_ids = tuple(repo.list_active_legacy_account_ids(platform_user_id))
        if not legacy_ids:
            return world
        if len(legacy_ids) > 1:
            logger.warning(
                "companion_world.bootstrap.multiple_legacy_accounts "
                "platform_user_id=%s count=%s carried=%s",
                platform_user_id,
                len(legacy_ids),
                legacy_ids[0],
            )
        repo.ensure_legacy_resident(world, legacy_ids[0])
        return repo.mark_legacy_primary(world.id, legacy_ids[0])

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
            active_count = repo.count_active_residents(world.id)
            # 「至少保留一位」按世界最终有人算：微信带入的 legacy 居民同样占名额、同样计数，
            # 所以老用户即使叉掉全部四位预设候选也能确认（D-A / Q2）。
            if active_count + len(to_activate) <= 0:
                raise CompanionWorldError("resident_capacity_empty")
            if active_count + len(to_activate) > MAX_ACTIVE_RESIDENTS:
                raise CompanionWorldError("resident_capacity_exceeded")

            for candidate, selection in chosen:
                if candidate.status == "active":
                    continue
                display_name = (selection.display_name or candidate.template.name).strip()
                if not display_name:
                    raise CompanionWorldError("resident_selection_invalid")
                # 用户自定义称呼走字符白名单 + 控制字符过滤；沿用模板名时不设限
                # （模板名是运营录入的可信值，可能超过用户输入的长度上限）。
                if selection.display_name and not is_valid_display_name(display_name):
                    raise CompanionWorldError("display_name_invalid")
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
            return self._create_resident_locked(
                repo,
                platform_user_id,
                template_id=template_id,
                custom_template=custom_template,
            )

    def _create_resident_locked(
        self,
        repo: WorldRepository,
        platform_user_id: str,
        *,
        template_id: Optional[str] = None,
        custom_template: Optional[TemplateDraft] = None,
    ) -> CandidateRecord | ResidentRecord:
        """create_resident 的事务内实现；草稿消费路径复用同一套锁序与容量规则。"""
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

    def preview_resident_draft(
        self,
        platform_user_id: str,
        persona: PersonaInput,
        *,
        draft_token: str,
        expires_at: str,
        safety_json: Optional[str] = None,
    ) -> Tuple[ResidentDraftRecord, RenderedPersona]:
        """渲染并落一条自建角色草稿。

        ``persona`` 里的自由文本必须**已在 API 入口过清洗器**（领域层不做 I/O，也就不做安全
        判定）。渲染结果与草稿一起落库，消费时原样使用，从而保证「所见即所存」。
        """
        world = self._repository.get_home_universe(platform_user_id)
        if world is None or world.onboarding_state == "preparing":
            raise CompanionWorldError("world_not_ready")
        self._ensure_world_available(world)
        # 预览阶段就先挡容量：让用户填完再告知已满是糟糕体验，且 confirmed 世界必然会失败。
        if (
            world.onboarding_state == "confirmed"
            and self._repository.count_active_residents(world.id) >= MAX_ACTIVE_RESIDENTS
        ):
            raise CompanionWorldError("resident_capacity_exceeded")
        try:
            rendered = render_persona(persona)
        except PersonaCatalogError as err:
            raise CompanionWorldError(err.code) from err

        draft = self._repository.create_resident_draft(
            platform_user_id,
            draft_token=draft_token,
            name=persona.name.strip(),
            avatar_key=persona.avatar_key,
            relationship_type=persona.relationship_type,
            relationship_label=persona.relationship_label,
            personality_traits=persona.personality_traits,
            style_note=persona.style_note,
            normalized_summary=rendered.normalized_summary,
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": rendered.soul_markdown,
                    "IDENTITY.md": rendered.identity_markdown,
                },
                ensure_ascii=False,
            ),
            safety_json=safety_json,
            expires_at=expires_at,
        )
        return draft, rendered

    def create_resident_from_draft(
        self,
        platform_user_id: str,
        *,
        draft_token: str,
        client_request_id: str,
        now: str,
    ) -> CandidateRecord | ResidentRecord:
        """消费草稿建居民；同一 ``client_request_id`` 重放返回原结果而不是报错（IDEM-001）。"""
        with self._repository.transaction() as repo:
            replay = repo.get_resident_draft_by_request(
                platform_user_id, client_request_id
            )
            if replay is not None and replay.resident_id:
                existing = self._find_resident_or_candidate(
                    repo, platform_user_id, replay.resident_id
                )
                if existing is not None:
                    return existing
                # 幂等行在，但居民已被叉掉/清理：回放没有意义，按草稿已消费处理。
                raise CompanionWorldError("resident_draft_consumed")

            draft = repo.get_resident_draft(draft_token, platform_user_id)
            if draft is None:
                raise CompanionWorldError("resident_draft_not_found")
            if draft.status != "open":
                raise CompanionWorldError("resident_draft_consumed")
            if draft.expires_at <= now:
                raise CompanionWorldError("resident_draft_expired")

            result = self._create_resident_locked(
                repo,
                platform_user_id,
                custom_template=TemplateDraft(
                    name=draft.name,
                    persona_seed_json=draft.persona_seed_json,
                    avatar_ref=resolve_avatar_ref(draft.avatar_key),
                    summary=draft.normalized_summary,
                    tags=tuple(
                        PERSONALITY_TRAITS[key]
                        for key in draft.personality_traits
                        if key in PERSONALITY_TRAITS
                    ),
                    relationship_type=draft.relationship_type,
                    personality_traits=draft.personality_traits,
                ),
            )
            resident_id = (
                result.resident_id
                if isinstance(result, CandidateRecord)
                else result.resident_id
            )
            if not repo.consume_resident_draft(
                draft.id, platform_user_id, client_request_id, resident_id
            ):
                # 同一草稿被并发消费；本笔整体回滚，客户端重放会命中幂等行。
                raise CompanionWorldError("resident_draft_consumed")
            return result

    @staticmethod
    def _find_resident_or_candidate(
        repo: WorldRepository, platform_user_id: str, resident_id: str
    ) -> Optional[CandidateRecord | ResidentRecord]:
        """幂等回放用：先找已激活居民，再找仍在候选态的那一位。"""
        for item in repo.list_residents_for_owner(
            platform_user_id, ("active", "offline")
        ):
            if item.resident_id == resident_id:
                return item
        world = repo.get_home_universe(platform_user_id)
        if world is None:
            return None
        for candidate in repo.list_candidates(world.id, ("candidate",)):
            if candidate.resident_id == resident_id:
                return candidate
        return None

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

    def list_conversations(
        self,
        platform_user_id: str,
        *,
        cursor_conversation_id: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[ConversationSummary, ...]:
        """列出 owner 自己的 AI conversations；cursor 越权只得到空页。"""
        return tuple(
            self._repository.list_conversations_for_owner(
                platform_user_id, cursor_conversation_id, limit
            )
        )

    def list_conversation_messages(
        self,
        platform_user_id: str,
        conversation_id: str,
        *,
        before_id: Optional[int] = None,
        limit: int = 50,
    ) -> Tuple[ConversationTarget, Tuple[ConversationMessage, ...]]:
        """owner 校验后读取目标 runtime 的跨日 App scope 历史。"""
        target = self.resolve_conversation(platform_user_id, conversation_id)
        messages = tuple(
            self._repository.list_conversation_messages(
                target.runtime_account_id, before_id, limit
            )
        )
        return target, messages
