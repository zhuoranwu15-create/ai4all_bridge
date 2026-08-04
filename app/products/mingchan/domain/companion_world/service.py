"""Companion World 纯领域编排（bootstrap / confirm / create / owner resolve）。"""
from __future__ import annotations

import json
from typing import Dict, Optional, Sequence, Tuple

from app.products.mingchan.domain.companion_world.contracts import (
    BootstrapResult,
    CandidateRecord,
    CompanionWorldError,
    ConversationMessage,
    ConversationReadState,
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
from app.products.mingchan.domain.companion_world.naming import select_suggested_name
from app.products.mingchan.domain.companion_world.onboarding_content import (
    intro_content_for_persona,
)
from app.products.mingchan.domain.companion_world.persona_catalog import (
    PERSONALITY_TRAITS,
    PersonaCatalogError,
    PersonaInput,
    RenderedPersona,
    is_valid_display_name,
    render_persona,
    resolve_avatar_ref,
)

MAX_ACTIVE_RESIDENTS = 10
# 初始候选目录的 rank 必须是**连续的 1..N**，且至少 MIN_INITIAL_CANDIDATES 位。
#
# CANDIDATE-001（v1.5，候选 4→5 加入司辰）刻意改成「连续 + 下限」而不是写死 (1,2,3,4)：
# 写死会让「代码期望 N 位」与「库里有 M 位」互为死锁——先改代码则选角页立刻
# preset_catalog_not_ready，先导数据则导入脚本拒收，必须精确编排两次发布。改成下限后，
# 加一位预设变成**纯数据操作**（导入新 manifest 即生效），代码无需再动。
#
# 原有保护未丢：少一位（(1,2,3)）撞下限、缺号（(1,2,4,5)）不连续，两种脏数据仍然硬失败。
MIN_INITIAL_CANDIDATES = 4


class CompanionWorldService:
    """按冻结规则编排 world；所有写操作都经 repository 单事务执行。"""

    def __init__(self, repository: WorldRepository, *, language: str = "zh-CN") -> None:
        self._repository = repository
        self._language = language

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
        expected = tuple(range(1, len(ordered) + 1))
        if (
            len(ordered) < MIN_INITIAL_CANDIDATES
            or ranks != expected
            or not metadata_ready
        ):
            raise CompanionWorldError("preset_catalog_not_ready")
        return ordered

    def bootstrap_home(self, platform_user_id: str) -> BootstrapResult:
        """幂等建立鸣蝉 home world，并在首次 preparing 时快照初始候选。"""
        with self._repository.transaction() as repo:
            world = repo.get_or_create_home_universe(platform_user_id)
            self._ensure_world_available(world)
            world = repo.lock_universe(world.id)
            if world.onboarding_state == "preparing":
                templates = self._validate_initial_catalog(repo.list_initial_templates())
                for template in templates:
                    # NAME-001：选名只发生在这一次快照，之后 ensure_candidate 命中已有行原样返回。
                    suggested = select_suggested_name(
                        universe_id=world.id,
                        template_id=template.id,
                        name_pool=template.name_pool,
                        name_pool_version=template.name_pool_version or "",
                    )
                    repo.ensure_candidate(
                        world.id,
                        template,
                        "preset",
                        suggested_display_name=suggested,
                        # 名池未配时不记版本，避免出现「有版本却没名字」的自相矛盾行。
                        naming_version=template.name_pool_version if suggested else None,
                    )
                world = repo.set_universe_onboarding_state(world.id, "selecting")
            candidates = tuple(repo.list_candidates(world.id, ("candidate",)))
            residents = tuple(repo.list_residents_for_owner(platform_user_id, ("active",)))
        return BootstrapResult(world=world, candidates=candidates, residents=residents)

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
                # 客户端不传称呼时用快照下来的实例名（NAME-001），没有名池才退回模板工作名。
                display_name = (
                    selection.display_name
                    or candidate.suggested_display_name
                    or candidate.template.name
                ).strip()
                if not display_name:
                    raise CompanionWorldError("resident_selection_invalid")
                # 用户自定义称呼走字符白名单 + 控制字符过滤；沿用模板名时不设限
                # （模板名是运营录入的可信值，可能超过用户输入的长度上限）。
                if selection.display_name and not is_valid_display_name(display_name):
                    raise CompanionWorldError("display_name_invalid")
                resident = repo.activate_candidate_with_runtime(
                    candidate,
                    owner_platform_user_id=platform_user_id,
                    display_name=display_name,
                )
                self._seed_resident_intro(repo, resident, candidate.template.persona_key)
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

        # 与 bootstrap 同一套选名规则；自建模板没有名池，这里恒得 None。
        suggested = select_suggested_name(
            universe_id=world.id,
            template_id=template.id,
            name_pool=template.name_pool,
            name_pool_version=template.name_pool_version or "",
        )
        candidate = repo.ensure_candidate(
            world.id,
            template,
            origin,
            suggested_display_name=suggested,
            naming_version=template.name_pool_version if suggested else None,
        )
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
        resident = repo.activate_candidate_with_runtime(
            candidate,
            owner_platform_user_id=platform_user_id,
            display_name=candidate.suggested_display_name or template.name,
        )
        self._seed_resident_intro(repo, resident, template.persona_key)
        return resident

    def _seed_resident_intro(
        self,
        repo: WorldRepository,
        resident: ResidentRecord,
        persona_key: Optional[str],
    ) -> None:
        """新居民落地即写一条欢迎语与一条自我介绍动态（CONTENT-001 / CONTENT-002）。

        查不到该人设的文案就整体跳过、不写兜底句——自建角色（persona_key 恒 None）与运营
        新加但还没配文案的预设都走这条路径，宁可少两条内容也不让通用文案顶上。
        """
        content = intro_content_for_persona(persona_key, language=self._language)
        if content is None:
            return
        repo.seed_resident_intro(
            resident,
            welcome_message=content.welcome_message,
            intro_post=content.intro_post,
        )

    def begin_wish_preview(
        self,
        platform_user_id: str,
        *,
        wish_request_id: str,
        window_start: str,
        daily_max: int,
    ) -> Optional[Tuple[ResidentDraftRecord, RenderedPersona]]:
        """许愿预览的**生成前**关卡（WISH-005）。

        返回非空即命中幂等重放：同一个 ``wish_request_id`` 恒等于同一份草稿，直接按存量行
        重新渲染回显——渲染是纯函数，重放响应与首次逐字段相同，且不会再调一次 LLM、
        不再占一次日额度。

        返回 None 表示这是一笔新许愿，调用方可以去生成。世界可用性与日额度都在**这里**
        先判，避免"花了一次 LLM 才发现世界没就绪/额度已满"。

        :raises CompanionWorldError: ``wish_rate_limited`` 及世界不可用类错误码。
        """
        replay = self._repository.get_resident_draft_by_wish_request(
            platform_user_id, wish_request_id
        )
        if replay is not None:
            return replay, self._render_draft(replay)

        world = self._repository.get_home_universe(platform_user_id)
        if world is None or world.onboarding_state == "preparing":
            raise CompanionWorldError("world_not_ready")
        self._ensure_world_available(world)
        if daily_max > 0 and (
            self._repository.count_wish_drafts_since(platform_user_id, window_start)
            >= daily_max
        ):
            raise CompanionWorldError("wish_rate_limited")
        return None

    @staticmethod
    def _render_draft(draft: ResidentDraftRecord) -> RenderedPersona:
        """由草稿行重新渲染回显字段。

        渲染是纯函数且草稿存的就是渲染输入，所以重放结果与首次预览必然一致——这正是
        「所见即所存」在重放路径上的体现，不需要把 tags/关系展示名再冗余存一份。
        """
        return render_persona(
            PersonaInput(
                name=draft.name,
                avatar_key=draft.avatar_key,
                relationship_type=draft.relationship_type,
                relationship_label=draft.relationship_label,
                personality_traits=tuple(draft.personality_traits),
                style_note=draft.style_note,
            )
        )

    def preview_resident_draft(
        self,
        platform_user_id: str,
        persona: PersonaInput,
        *,
        draft_token: str,
        expires_at: str,
        safety_json: Optional[str] = None,
        source: str = "form",
        wish_request_id: Optional[str] = None,
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
            source=source,
            wish_request_id=wish_request_id,
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

    def mark_conversation_read(
        self, platform_user_id: str, conversation_id: str, *, last_message_id: int
    ) -> ConversationReadState:
        """推进已读游标；越权与不存在同样是 conversation_not_found，不泄漏他人会话存在性。"""
        state = self._repository.advance_conversation_read_cursor(
            conversation_id, platform_user_id, last_message_id
        )
        if state is None:
            raise CompanionWorldError("conversation_not_found")
        return state
