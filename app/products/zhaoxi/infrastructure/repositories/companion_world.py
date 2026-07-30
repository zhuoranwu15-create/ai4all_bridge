"""Companion World 的 SQL adapter（SQLite/PG 共用，事务由 connect() 承载）。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence, Tuple

from app.config import settings
from app.db._backend import Connection, is_postgres
from app.db._core import connect
from app.db.billing import insert_resident_runtime_account
from app.products.zhaoxi.domain.companion_world.contracts import (
    CandidateRecord,
    CompanionWorldError,
    ConversationMessage,
    ConversationReadState,
    ConversationSummary,
    ConversationTarget,
    ResidentDraftRecord,
    ResidentRecord,
    TemplateDraft,
    TemplateRecord,
    UniversePostRecord,
    WorldRecord,
    WorldRepository,
)
from app.products.zhaoxi.domain.companion_world.proactive import (
    HumanProactiveScope,
    HumanProactiveSpeaker,
    decide_human_proactive_delivery,
)
from app.products.zhaoxi.infrastructure.persistence import companion_world as world_db
from app.time_utils import beijing_now_str


HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON = (
    "companion_world_human_level_proactive_blocked"
)


def _decode_tags(raw: Optional[str]) -> Tuple[str, ...]:
    try:
        value = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _resident_draft(row: dict) -> ResidentDraftRecord:
    return ResidentDraftRecord(
        id=str(row["id"]),
        platform_user_id=str(row["platform_user_id"]),
        draft_token=str(row["draft_token"]),
        name=str(row["name"]),
        avatar_key=str(row["avatar_key"]),
        relationship_type=str(row["relationship_type"]),
        relationship_label=row.get("relationship_label"),
        personality_traits=_decode_tags(row.get("personality_traits_json")),
        style_note=row.get("style_note"),
        normalized_summary=str(row["normalized_summary"]),
        persona_seed_json=str(row["persona_seed_json"]),
        status=str(row["status"]),
        client_request_id=row.get("client_request_id"),
        resident_id=row.get("resident_id"),
        expires_at=str(row["expires_at"]),
    )


def _world(row: dict) -> WorldRecord:
    return WorldRecord(
        id=str(row["id"]),
        owner_platform_user_id=str(row["owner_platform_user_id"]),
        status=str(row["status"]),
        onboarding_state=str(row["onboarding_state"]),
        legacy_primary_account_id=row.get("legacy_primary_account_id"),
    )


def _template(row: dict) -> TemplateRecord:
    return TemplateRecord(
        id=str(row.get("character_template_id") or row["id"]),
        source_type=str(row["source_type"]),
        owner_platform_user_id=row.get("owner_platform_user_id"),
        name=str(row["name"]),
        avatar_ref=row.get("avatar_ref"),
        summary=row.get("summary"),
        tags=_decode_tags(row.get("tags_json")),
        persona_seed_json=row.get("persona_seed_json"),
        persona_version=str(row["persona_version"]),
        status=str(row.get("template_status") or row["status"]),
        initial_candidate_rank=(
            int(row["initial_candidate_rank"])
            if row.get("initial_candidate_rank") is not None
            else None
        ),
        persona_key=row.get("persona_key"),
        long_summary=row.get("long_summary"),
        name_pool=_decode_tags(row.get("name_pool_json")),
        name_pool_version=row.get("name_pool_version"),
    )


def _candidate(row: dict) -> CandidateRecord:
    return CandidateRecord(
        resident_id=str(row["resident_id"]),
        universe_id=str(row["universe_id"]),
        template=_template(row),
        template_version=str(row["template_version"]),
        origin=str(row["origin"]),
        status=str(row["status"]),
        runtime_account_id=row.get("runtime_account_id"),
        conversation_id=row.get("conversation_id"),
        suggested_display_name=row.get("suggested_display_name"),
        naming_version=row.get("naming_version"),
    )


def _resident(row: dict) -> ResidentRecord:
    runtime_account_id = row.get("runtime_account_id")
    conversation_id = row.get("conversation_id")
    if not runtime_account_id or not conversation_id:
        raise RuntimeError("active resident is missing runtime/conversation")
    return ResidentRecord(
        resident_id=str(row["resident_id"]),
        universe_id=str(row["universe_id"]),
        template_id=str(row["character_template_id"]),
        template_version=str(row["template_version"]),
        runtime_account_id=str(runtime_account_id),
        origin=str(row["origin"]),
        status=str(row["status"]),
        name=str(row["name"]),
        avatar_ref=row.get("avatar_ref"),
        conversation_id=str(conversation_id),
        conversation_state=str(row.get("conversation_state") or "active"),
        persona_key=row.get("persona_key"),
    )


def _feed_post(row: dict) -> UniversePostRecord:
    """把 owner-scoped Feed projection 转成不泄漏存储字段的领域 DTO。"""
    return UniversePostRecord(
        id=str(row["id"]),
        universe_id=str(row["universe_id"]),
        author_type=str(row["author_type"]),
        source_type=str(row["source_type"]),
        status=str(row["status"]),
        text=row.get("text"),
        author_platform_user_id=row.get("author_platform_user_id"),
        author_resident_id=row.get("author_resident_id"),
        author_name=row.get("author_name"),
        author_avatar_ref=row.get("author_avatar_ref"),
        published_at=row.get("published_at"),
        post_type=str(row.get("post_type") or "normal"),
        terminal_reason=row.get("terminal_reason"),
        media_ids=tuple(str(mid) for mid in (row.get("media_ids") or ())),
    )


def _persona_parts(raw: Optional[str]) -> Tuple[str, str, str]:
    """解析受控 persona seed；兼容 ``soul`` 与 ``SOUL.md`` 两种键名。"""
    if not raw:
        return "", "", ""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as err:
        raise ValueError("invalid persona_seed_json") from err
    if not isinstance(value, dict):
        raise ValueError("invalid persona_seed_json")
    soul = value.get("SOUL.md", value.get("soul", ""))
    identity = value.get("IDENTITY.md", value.get("identity", ""))
    system_prompt = value.get("system_prompt", "")
    if not all(isinstance(item, str) for item in (soul, identity, system_prompt)):
        raise ValueError("invalid persona_seed_json")
    return soul.strip(), identity.strip(), system_prompt.strip()


class SqlCompanionWorldRepository(WorldRepository):
    """把领域 repository 端口映射到现有 DB 原语；绑定实例的所有写共享一条连接。"""

    def __init__(self, conn: Optional[Connection] = None) -> None:
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[WorldRepository]:
        """开启单事务并返回绑定 repository；已有绑定时复用外层事务。"""
        if self._conn is not None:
            yield self
            return
        with connect() as conn:
            # SQLite 的 SELECT 不自动开事务，且最外层 SAVEPOINT RELEASE 会提前提交；
            # BEGIN IMMEDIATE 同时提供 world 写串行与完整 rollback 边界。PG 由驱动自动 BEGIN。
            if not is_postgres():
                conn.execute("BEGIN IMMEDIATE")
            yield SqlCompanionWorldRepository(conn)

    def _required_conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("operation requires repository.transaction()")
        return self._conn

    def get_or_create_home_universe(self, platform_user_id: str) -> WorldRecord:
        return _world(
            world_db.get_or_create_home_universe(
                platform_user_id=platform_user_id, conn=self._conn
            )
        )

    def get_home_universe(self, platform_user_id: str) -> Optional[WorldRecord]:
        row = world_db.get_universe(
            owner_platform_user_id=platform_user_id, conn=self._conn
        )
        return _world(row) if row else None

    def get_post_for_owner(
        self, post_id: str, platform_user_id: str
    ) -> Optional[UniversePostRecord]:
        row = world_db.get_universe_post_for_owner(
            post_id=post_id,
            owner_platform_user_id=platform_user_id,
            conn=self._conn,
        )
        return _feed_post(row) if row else None

    def publish_user_post(
        self,
        *,
        platform_user_id: str,
        client_request_id: str,
        text: str,
        request_fingerprint: str,
        published_at: str,
        media_ids: Sequence[str] = (),
    ) -> Tuple[UniversePostRecord, bool]:
        try:
            row, created = world_db.publish_user_feed_post_with_outbox(
                owner_platform_user_id=platform_user_id,
                client_request_id=client_request_id,
                text=text,
                request_fingerprint=request_fingerprint,
                published_at=published_at,
                media_ids=tuple(media_ids),
                conn=self._conn,
            )
        except ValueError as err:
            code = str(err)
            if code in {
                "world_not_ready",
                "world_disabled",
                "idempotency_conflict",
                "media_ref_invalid",
            }:
                raise CompanionWorldError(code) from err
            raise
        return _feed_post(row), created

    def list_published_posts(
        self,
        *,
        platform_user_id: str,
        cursor_published_at: Optional[str],
        cursor_post_id: Optional[str],
        limit: int,
    ) -> Sequence[UniversePostRecord]:
        try:
            rows = world_db.list_published_feed_posts_for_owner(
                owner_platform_user_id=platform_user_id,
                cursor_published_at=cursor_published_at,
                cursor_post_id=cursor_post_id,
                limit=limit,
                conn=self._conn,
            )
        except ValueError as err:
            code = str(err)
            if code in {"world_not_ready", "world_disabled", "invalid_cursor"}:
                raise CompanionWorldError(code) from err
            raise
        return tuple(_feed_post(row) for row in rows)

    def retire_post(
        self,
        *,
        platform_user_id: str,
        post_id: str,
        reason_code: str,
        expected_author_type: str,
        forbid_post_types: Sequence[str],
        retired_at: str,
    ) -> Tuple[UniversePostRecord, bool]:
        try:
            row, changed = world_db.retire_feed_post_with_outbox(
                owner_platform_user_id=platform_user_id,
                post_id=post_id,
                reason_code=reason_code,
                deleted_at=retired_at,
                expected_author_type=expected_author_type,
                forbid_post_types=tuple(forbid_post_types),
                conn=self._conn,
            )
        except ValueError as err:
            code = str(err)
            # 只把已冻结的业务码翻译成领域错误；其余（如 outbox 冲突）是真故障，不能被
            # 降级成一个客户端可分支的 4xx。
            if code in {"post_not_found", "post_not_hideable"}:
                raise CompanionWorldError(code) from err
            raise
        return _feed_post(row), changed

    def lock_universe(self, universe_id: str) -> WorldRecord:
        row = world_db.lock_universe(
            universe_id=universe_id, conn=self._required_conn()
        )
        if row is None:
            raise RuntimeError("universe disappeared while locking")
        return _world(row)

    def set_universe_onboarding_state(
        self, universe_id: str, onboarding_state: str
    ) -> WorldRecord:
        row = world_db.set_universe_onboarding_state(
            universe_id=universe_id,
            onboarding_state=onboarding_state,
            conn=self._conn,
        )
        if row is None:
            raise RuntimeError("universe not found")
        return _world(row)

    def list_initial_templates(self) -> Sequence[TemplateRecord]:
        return tuple(
            _template(row)
            for row in world_db.list_initial_character_templates(conn=self._conn)
        )

    def get_available_template(
        self, template_id: str, owner_platform_user_id: str
    ) -> Optional[TemplateRecord]:
        row = world_db.get_available_character_template(
            template_id=template_id,
            owner_platform_user_id=owner_platform_user_id,
            conn=self._conn,
        )
        return _template(row) if row else None

    def get_template_for_owner(
        self, template_id: str, owner_platform_user_id: str
    ) -> Optional[TemplateRecord]:
        row = world_db.get_character_template_for_owner(
            template_id=template_id,
            owner_platform_user_id=owner_platform_user_id,
            conn=self._conn,
        )
        return _template(row) if row else None

    def create_custom_template(
        self, owner_platform_user_id: str, draft: TemplateDraft
    ) -> TemplateRecord:
        row = world_db.create_character_template(
            source_type="user_created",
            owner_platform_user_id=owner_platform_user_id,
            name=draft.name,
            avatar_ref=draft.avatar_ref,
            summary=draft.summary,
            tags_json=json.dumps(list(draft.tags), ensure_ascii=False),
            persona_seed_json=draft.persona_seed_json,
            persona_version=draft.persona_version,
            relationship_type=draft.relationship_type,
            personality_traits_json=(
                json.dumps(list(draft.personality_traits), ensure_ascii=False)
                if draft.personality_traits
                else None
            ),
            conn=self._conn,
        )
        return _template(row)

    def create_resident_draft(
        self,
        platform_user_id: str,
        *,
        draft_token: str,
        name: str,
        avatar_key: str,
        relationship_type: str,
        relationship_label: Optional[str],
        personality_traits: Sequence[str],
        style_note: Optional[str],
        normalized_summary: str,
        persona_seed_json: str,
        safety_json: Optional[str],
        expires_at: str,
        source: str = "form",
        wish_request_id: Optional[str] = None,
    ) -> ResidentDraftRecord:
        row = world_db.insert_resident_draft(
            platform_user_id=platform_user_id,
            draft_token=draft_token,
            name=name,
            avatar_key=avatar_key,
            relationship_type=relationship_type,
            relationship_label=relationship_label,
            personality_traits_json=json.dumps(
                list(personality_traits), ensure_ascii=False
            ),
            style_note=style_note,
            normalized_summary=normalized_summary,
            persona_seed_json=persona_seed_json,
            safety_json=safety_json,
            expires_at=expires_at,
            source=source,
            wish_request_id=wish_request_id,
            conn=self._conn,
        )
        return _resident_draft(row)

    def get_resident_draft_by_wish_request(
        self, platform_user_id: str, wish_request_id: str
    ) -> Optional[ResidentDraftRecord]:
        row = world_db.get_resident_draft_by_wish_request(
            platform_user_id=platform_user_id,
            wish_request_id=wish_request_id,
            conn=self._conn,
        )
        return _resident_draft(row) if row else None

    def count_wish_drafts_since(self, platform_user_id: str, since: str) -> int:
        return world_db.count_resident_drafts_since(
            platform_user_id=platform_user_id,
            source="wish",
            since=since,
            conn=self._conn,
        )

    def get_resident_draft(
        self, draft_token: str, platform_user_id: str
    ) -> Optional[ResidentDraftRecord]:
        row = world_db.get_resident_draft_by_token(
            draft_token=draft_token,
            platform_user_id=platform_user_id,
            conn=self._conn,
        )
        return _resident_draft(row) if row else None

    def get_resident_draft_by_request(
        self, platform_user_id: str, client_request_id: str
    ) -> Optional[ResidentDraftRecord]:
        row = world_db.get_resident_draft_by_client_request(
            platform_user_id=platform_user_id,
            client_request_id=client_request_id,
            conn=self._conn,
        )
        return _resident_draft(row) if row else None

    def consume_resident_draft(
        self,
        draft_id: str,
        platform_user_id: str,
        client_request_id: str,
        resident_id: str,
    ) -> bool:
        return world_db.consume_resident_draft(
            draft_id=draft_id,
            platform_user_id=platform_user_id,
            client_request_id=client_request_id,
            resident_id=resident_id,
            conn=self._conn,
        )

    def ensure_candidate(
        self,
        universe_id: str,
        template: TemplateRecord,
        origin: str,
        *,
        suggested_display_name: Optional[str] = None,
        naming_version: Optional[str] = None,
    ) -> CandidateRecord:
        row = world_db.get_or_create_candidate_resident(
            universe_id=universe_id,
            character_template_id=template.id,
            template_version=template.persona_version,
            origin=origin,
            suggested_display_name=suggested_display_name,
            naming_version=naming_version,
            conn=self._conn,
        )
        candidates = self.list_candidates(
            universe_id, (str(row["status"]),)
        )
        for candidate in candidates:
            if candidate.resident_id == row["id"]:
                return candidate
        raise RuntimeError("candidate details were not found")

    def list_candidates(
        self, universe_id: str, statuses: Sequence[str]
    ) -> Sequence[CandidateRecord]:
        return tuple(
            _candidate(row)
            for row in world_db.list_candidate_residents(
                universe_id=universe_id, statuses=statuses, conn=self._conn
            )
        )

    def count_active_residents(self, universe_id: str) -> int:
        return world_db.count_active_residents(
            universe_id=universe_id, conn=self._conn
        )

    def activate_candidate_with_runtime(
        self,
        candidate: CandidateRecord,
        owner_platform_user_id: str,
        display_name: str,
    ) -> ResidentRecord:
        conn = self._required_conn()
        world = world_db.get_universe(
            universe_id=candidate.universe_id, conn=conn
        )
        if world is None or str(world["owner_platform_user_id"]) != str(
            owner_platform_user_id
        ):
            raise ValueError("candidate universe ownership mismatch")
        soul, identity, system_prompt = _persona_parts(
            candidate.template.persona_seed_json
        )
        runtime = insert_resident_runtime_account(
            platform_user_id=owner_platform_user_id,
            display_name=display_name,
            system_prompt=system_prompt,
            soul_seed=soul,
            identity_seed=identity,
            conn=conn,
        )
        account_id = str(runtime["account"]["id"])
        activated = world_db.activate_candidate_resident(
            universe_id=candidate.universe_id,
            resident_id=candidate.resident_id,
            runtime_account_id=account_id,
            conn=conn,
        )
        if activated is None:
            raise RuntimeError("candidate activation failed")
        world_db.create_ai_conversation(
            universe_id=candidate.universe_id,
            resident_id=candidate.resident_id,
            owner_platform_user_id=owner_platform_user_id,
            runtime_account_id=account_id,
            conn=conn,
        )
        for resident in self.list_residents_for_owner(
            owner_platform_user_id, ("active", "offline")
        ):
            if resident.resident_id == candidate.resident_id:
                return resident
        raise RuntimeError("activated resident details were not found")

    def seed_resident_intro(
        self,
        resident: ResidentRecord,
        *,
        welcome_message: str,
        intro_post: str,
    ) -> None:
        """在激活同事务里落一条欢迎语与一条自我介绍动态（CONTENT-001 / CONTENT-002）。

        文案由领域层挑好后传入，本层不认识 persona_key、也不持有兜底文案——决定"发什么"
        是产品口径，决定"怎么落"才是这里的职责。两条写入都幂等，重放不会重复。
        """
        conn = self._required_conn()
        world_db.insert_resident_welcome_message(
            runtime_account_id=resident.runtime_account_id,
            resident_id=resident.resident_id,
            text=welcome_message,
            conn=conn,
        )
        world_db.publish_resident_intro_post_with_outbox(
            universe_id=resident.universe_id,
            author_resident_id=resident.resident_id,
            text=intro_post,
            published_at=beijing_now_str(),
            conn=conn,
        )

    def dismiss_unselected_candidates(
        self, universe_id: str, selected_template_ids: Sequence[str]
    ) -> None:
        world_db.dismiss_unselected_candidate_residents(
            universe_id=universe_id,
            selected_template_ids=selected_template_ids,
            conn=self._conn,
        )

    def list_residents_for_owner(
        self, platform_user_id: str, statuses: Sequence[str]
    ) -> Sequence[ResidentRecord]:
        return tuple(
            _resident(row)
            for row in world_db.list_resident_details_for_owner(
                owner_platform_user_id=platform_user_id,
                statuses=statuses,
                conn=self._conn,
            )
        )

    def resolve_conversation_for_owner(
        self, conversation_id: str, platform_user_id: str
    ) -> Optional[ConversationTarget]:
        row = world_db.resolve_conversation_for_owner(
            conversation_id=conversation_id,
            owner_platform_user_id=platform_user_id,
            conn=self._conn,
        )
        if row is None:
            return None
        return ConversationTarget(
            conversation_id=str(row["conversation_id"]),
            universe_id=str(row["universe_id"]),
            resident_id=str(row["resident_id"]),
            owner_platform_user_id=str(row["owner_platform_user_id"]),
            runtime_account_id=str(row["runtime_account_id"]),
            state=str(row["state"]),
        )

    def list_conversations_for_owner(
        self,
        platform_user_id: str,
        cursor_conversation_id: Optional[str],
        limit: int,
    ) -> Sequence[ConversationSummary]:
        rows = world_db.list_conversations_for_owner(
            owner_platform_user_id=platform_user_id,
            cursor_conversation_id=cursor_conversation_id,
            limit=limit,
            conn=self._conn,
        )
        return tuple(
            ConversationSummary(
                conversation_id=str(row["conversation_id"]),
                resident_id=str(row["resident_id"]),
                resident_name=str(row["resident_name"]),
                resident_avatar_ref=row.get("avatar_ref"),
                resident_status=str(row["resident_status"]),
                state=str(row["state"]),
                last_preview=row.get("last_preview"),
                unread=int(row.get("unread") or 0),
                last_message_at=row.get("last_message_at"),
                sort_time=row.get("updated_at"),
            )
            for row in rows
        )

    def advance_conversation_read_cursor(
        self,
        conversation_id: str,
        platform_user_id: str,
        last_message_id: int,
    ) -> Optional[ConversationReadState]:
        row = world_db.advance_conversation_read_cursor(
            conversation_id=conversation_id,
            owner_platform_user_id=platform_user_id,
            last_message_id=last_message_id,
            conn=self._conn,
        )
        if row is None:
            return None
        cursor = row.get("last_read_message_id")
        return ConversationReadState(
            last_read_message_id=int(cursor) if cursor else None,
            unread=int(row.get("unread") or 0),
        )

    def list_conversation_messages(
        self,
        runtime_account_id: str,
        before_id: Optional[int],
        limit: int,
    ) -> Sequence[ConversationMessage]:
        from app.db.accounts import list_app_conversation_messages_before

        rows = list_app_conversation_messages_before(
            runtime_account_id=runtime_account_id,
            before_id=before_id,
            limit=limit,
            conn=self._conn,
        )
        return tuple(
            ConversationMessage(
                id=int(row["id"]),
                message_id=row.get("message_id"),
                role=str(row["role"]),
                message_type=str(row["message_type"]),
                content=str(row["content"]),
                created_at=str(row["created_at"]),
                content_json=row.get("content_json"),
                media_id=row.get("media_id"),
            )
            for row in rows
        )

    def list_active_legacy_account_ids(self, platform_user_id: str) -> Sequence[str]:
        return tuple(
            world_db.list_active_account_ids_for_user(
                platform_user_id=platform_user_id, conn=self._conn
            )
        )

    def allows_human_level_proactive(self, runtime_account_id: str) -> bool:
        """form-A 放行；world 按微信优先/App-only 双 flag/唯一 speaker 放行。"""
        scope = self.get_human_proactive_scope(runtime_account_id)
        if scope is None:
            return True
        decision = decide_human_proactive_delivery(
            legacy_weixin_route_available=scope.legacy_weixin_route_available,
            app_inbox_enabled=bool(
                getattr(settings, "companion_world_app_inbox_enabled", False)
            ),
            app_only_human_enabled=bool(
                getattr(
                    settings,
                    "companion_world_app_only_human_proactive_enabled",
                    False,
                )
            ),
            has_app_speaker=scope.app_speaker is not None,
        )
        if decision.mode == "weixin":
            return bool(scope.legacy_primary_account_id) and str(
                scope.legacy_primary_account_id
            ) == str(runtime_account_id)
        if decision.mode == "app_inbox":
            return bool(scope.app_speaker) and str(
                scope.app_speaker.runtime_account_id
            ) == str(runtime_account_id)
        return False

    def get_human_proactive_scope(
        self, runtime_account_id: str
    ) -> Optional[HumanProactiveScope]:
        """解析真人级 owner/account 聚合、微信路由与确定性 App speaker。"""

        row = world_db.get_human_proactive_owner_scope(
            runtime_account_id=runtime_account_id, conn=self._conn
        )
        if row is None:
            return None
        platform_user_id = str(row["owner_platform_user_id"])
        universe_id = str(row["universe_id"])
        speaker_row = world_db.select_human_app_speaker(
            platform_user_id=platform_user_id,
            universe_id=universe_id,
            conn=self._conn,
        )
        speaker = (
            HumanProactiveSpeaker(
                resident_id=str(speaker_row["resident_id"]),
                runtime_account_id=str(speaker_row["runtime_account_id"]),
                conversation_id=str(speaker_row["conversation_id"]),
                last_inbound_at=speaker_row.get("last_inbound_at"),
                last_app_activity_at=speaker_row.get("last_app_activity_at"),
                joined_at=speaker_row.get("joined_at"),
            )
            if speaker_row
            else None
        )
        return HumanProactiveScope(
            platform_user_id=platform_user_id,
            universe_id=universe_id,
            runtime_account_ids=tuple(
                world_db.list_human_proactive_account_ids_for_user(
                    platform_user_id=platform_user_id, conn=self._conn
                )
            ),
            requested_resident_id=str(row["resident_id"]),
            owner_created_at=str(row["owner_created_at"]),
            legacy_primary_account_id=row.get("legacy_primary_account_id"),
            legacy_weixin_route_available=world_db.has_legacy_primary_weixin_route(
                universe_id=universe_id, conn=self._conn
            ),
            app_speaker=speaker,
        )

    def ensure_legacy_resident(
        self, world: WorldRecord, runtime_account_id: str
    ) -> ResidentRecord:
        conn = self._required_conn()
        resident = world_db.get_or_create_legacy_resident(
            universe_id=world.id,
            runtime_account_id=runtime_account_id,
            conn=conn,
        )
        world_db.create_ai_conversation(
            universe_id=world.id,
            resident_id=str(resident["id"]),
            owner_platform_user_id=world.owner_platform_user_id,
            runtime_account_id=runtime_account_id,
            conn=conn,
        )
        for item in self.list_residents_for_owner(
            world.owner_platform_user_id, ("active",)
        ):
            if item.resident_id == resident["id"]:
                return item
        raise RuntimeError("legacy resident details were not found")

    def mark_legacy_primary(
        self, universe_id: str, legacy_primary_account_id: str
    ) -> WorldRecord:
        row = world_db.mark_universe_legacy_primary(
            universe_id=universe_id,
            legacy_primary_account_id=legacy_primary_account_id,
            conn=self._conn,
        )
        if row is None:
            raise RuntimeError("universe not found")
        return _world(row)

    def mark_legacy_world(
        self, universe_id: str, legacy_primary_account_id: str
    ) -> WorldRecord:
        row = world_db.mark_universe_legacy_confirmed(
            universe_id=universe_id,
            legacy_primary_account_id=legacy_primary_account_id,
            conn=self._conn,
        )
        if row is None:
            raise RuntimeError("universe not found")
        return _world(row)


def human_level_proactive_allowed(runtime_account_id: str) -> bool:
    """返回某 runtime account 是否可承担真人级主动触达。"""
    if not bool(getattr(settings, "companion_world_proactive_safety_enabled", True)):
        return True
    return SqlCompanionWorldRepository().allows_human_level_proactive(
        runtime_account_id
    )


def resolve_human_proactive_scope(
    runtime_account_id: str,
) -> Optional[HumanProactiveScope]:
    """公开 infrastructure helper：解析 world 真人级聚合 scope。"""

    return SqlCompanionWorldRepository().get_human_proactive_scope(
        runtime_account_id
    )


def human_level_app_route(runtime_account_id: str) -> Optional[dict]:
    """当且仅当双 flag 开启且无真实微信路由时返回原生 App inbox route。"""

    scope = resolve_human_proactive_scope(runtime_account_id)
    if scope is None or scope.app_speaker is None:
        return None
    decision = decide_human_proactive_delivery(
        legacy_weixin_route_available=scope.legacy_weixin_route_available,
        app_inbox_enabled=bool(
            getattr(settings, "companion_world_app_inbox_enabled", False)
        ),
        app_only_human_enabled=bool(
            getattr(
                settings,
                "companion_world_app_only_human_proactive_enabled",
                False,
            )
        ),
        has_app_speaker=True,
    )
    if decision.mode != "app_inbox" or (
        scope.app_speaker.runtime_account_id != runtime_account_id
    ):
        return None
    return {
        "channel_binding_id": None,
        "channel": "native",
        "channel_account_id": None,
        "to_user_id": scope.platform_user_id,
        "session_key": "__app_active__",
        "delivery": "app_inbox",
        "conversation_id": scope.app_speaker.conversation_id,
        "resident_id": scope.app_speaker.resident_id,
    }


def get_human_proactive_last_inbound_at(runtime_account_id: str) -> Optional[str]:
    """world 返回 owner 聚合 last inbound；form-A 返回 None 供调用方沿用旧查询。"""

    scope = resolve_human_proactive_scope(runtime_account_id)
    if scope is None:
        return None
    return world_db.get_owner_last_inbound_at(
        platform_user_id=scope.platform_user_id
    )


def count_human_proactive_inbound_after(
    runtime_account_id: str, *, after: str
) -> Optional[int]:
    """world 返回 owner 聚合入站数；form-A 返回 None。"""

    scope = resolve_human_proactive_scope(runtime_account_id)
    if scope is None:
        return None
    return world_db.count_owner_inbound_after(
        platform_user_id=scope.platform_user_id, after=after
    )


__all__ = [
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON",
    "SqlCompanionWorldRepository",
    "count_human_proactive_inbound_after",
    "get_human_proactive_last_inbound_at",
    "human_level_app_route",
    "human_level_proactive_allowed",
    "resolve_human_proactive_scope",
]
