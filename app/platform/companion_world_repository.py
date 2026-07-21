"""Companion World 的 SQL adapter（SQLite/PG 共用，事务由 connect() 承载）。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence, Tuple

from app.db import companion_world as world_db
from app.db._backend import Connection, is_postgres
from app.db._core import connect
from app.db.billing import insert_resident_runtime_account
from app.domains.companion_world.contracts import (
    CandidateRecord,
    ConversationMessage,
    ConversationSummary,
    ConversationTarget,
    ResidentRecord,
    TemplateDraft,
    TemplateRecord,
    WorldRecord,
    WorldRepository,
)


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
            conn=self._conn,
        )
        return _template(row)

    def ensure_candidate(
        self, universe_id: str, template: TemplateRecord, origin: str
    ) -> CandidateRecord:
        row = world_db.get_or_create_candidate_resident(
            universe_id=universe_id,
            character_template_id=template.id,
            template_version=template.persona_version,
            origin=origin,
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
            )
            for row in rows
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
        """form-A 放行；world resident 仅 legacy primary 可做人级主动触达。"""
        scope = world_db.resolve_resident_proactive_scope(
            runtime_account_id=runtime_account_id,
            conn=self._conn,
        )
        if scope is None:
            return True
        primary_account_id = scope.get("legacy_primary_account_id")
        return bool(primary_account_id) and str(primary_account_id) == str(
            runtime_account_id
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
    return SqlCompanionWorldRepository().allows_human_level_proactive(
        runtime_account_id
    )


__all__ = [
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON",
    "SqlCompanionWorldRepository",
    "human_level_proactive_allowed",
]
