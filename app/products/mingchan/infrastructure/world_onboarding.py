"""鸣蝉 World onboarding 的 SQLite/PostgreSQL repository。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence, Tuple

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db._backend import Connection, is_postgres
from app.db._core import connect
from app.db.product_memberships import _require_active_product_membership_in_conn
from app.products.mingchan.domain.companion_world.onboarding import (
    CandidateRecord,
    MingchanWorldError,
    MingchanWorldOnboardingRepository,
    ResidentRecord,
    TemplateRecord,
    WorldRecord,
)
from app.products.mingchan.infrastructure.accounts import (
    insert_mingchan_resident_runtime_account,
)
from app.products.mingchan.infrastructure.persistence import companion_world as world_db
from app.time_utils import beijing_now_str


def _decode_tuple(raw: Optional[str]) -> Tuple[str, ...]:
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
    )


def _template(row: dict) -> TemplateRecord:
    return TemplateRecord(
        id=str(row.get("character_template_id") or row["id"]),
        source_type=str(row["source_type"]),
        owner_platform_user_id=row.get("owner_platform_user_id"),
        name=str(row["name"]),
        avatar_ref=row.get("avatar_ref"),
        summary=row.get("summary"),
        tags=_decode_tuple(row.get("tags_json")),
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
        name_pool=_decode_tuple(row.get("name_pool_json")),
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


def _persona_parts(raw: Optional[str]) -> Tuple[str, str, str]:
    try:
        value = json.loads(raw or "")
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


class SqlMingchanWorldOnboardingRepository(MingchanWorldOnboardingRepository):
    """将鸣蝉 onboarding 端口映射到产品 persistence，并强制产品归属。"""

    def __init__(
        self,
        *,
        registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
        conn: Optional[Connection] = None,
    ) -> None:
        self._registry = registry
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[MingchanWorldOnboardingRepository]:
        """开启单事务；PG 依赖驱动事务，SQLite 预先获得 writer 锁。"""

        if self._conn is not None:
            yield self
            return
        with connect() as conn:
            if not is_postgres():
                conn.execute("BEGIN IMMEDIATE")
            yield SqlMingchanWorldOnboardingRepository(
                registry=self._registry,
                conn=conn,
            )

    def _required_conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("operation requires repository.transaction()")
        return self._conn

    def _require_owner_membership(self, platform_user_id: str) -> None:
        _require_active_product_membership_in_conn(
            self._required_conn(),
            platform_user_id=platform_user_id,
            app_id=MINGCHAN_APP_ID,
            registry=self._registry,
        )

    def get_or_create_home_universe(self, platform_user_id: str) -> WorldRecord:
        """校验鸣蝉 membership 后幂等取得 home world。"""

        self._require_owner_membership(platform_user_id)
        try:
            row = world_db.get_or_create_home_universe(
                platform_user_id=platform_user_id,
                app_id=MINGCHAN_APP_ID,
                conn=self._required_conn(),
            )
        except world_db.UniverseOwnerProductConflictError as err:
            raise MingchanWorldError("legacy_world_cleanup_required") from err
        return _world(row)

    def get_home_universe(self, platform_user_id: str) -> Optional[WorldRecord]:
        """只允许 active 鸣蝉 member 读取自己的 home world。"""

        self._require_owner_membership(platform_user_id)
        row = world_db.get_universe(
            owner_platform_user_id=platform_user_id,
            expected_app_id=MINGCHAN_APP_ID,
            conn=self._required_conn(),
        )
        return _world(row) if row else None

    def lock_universe(self, universe_id: str) -> WorldRecord:
        """锁定当前事务中的 universe。"""

        row = world_db.lock_universe(
            universe_id=universe_id,
            expected_app_id=MINGCHAN_APP_ID,
            conn=self._required_conn(),
        )
        if row is None:
            raise RuntimeError("universe disappeared while locking")
        return _world(row)

    def set_universe_onboarding_state(
        self, universe_id: str, onboarding_state: str
    ) -> WorldRecord:
        """更新 onboarding 状态并返回最新 world。"""

        row = world_db.set_universe_onboarding_state(
            universe_id=universe_id,
            onboarding_state=onboarding_state,
            conn=self._required_conn(),
        )
        if row is None:
            raise RuntimeError("universe not found")
        return _world(row)

    def list_initial_templates(self) -> Sequence[TemplateRecord]:
        """读取鸣蝉当前的初始候选模板目录。"""

        return tuple(
            _template(row)
            for row in world_db.list_initial_character_templates(
                app_id=MINGCHAN_APP_ID,
                conn=self._required_conn()
            )
        )

    def ensure_candidate(
        self,
        universe_id: str,
        template: TemplateRecord,
        *,
        suggested_display_name: Optional[str],
        naming_version: Optional[str],
    ) -> CandidateRecord:
        """为 world 幂等快照一个预设候选。"""

        row = world_db.get_or_create_candidate_resident(
            universe_id=universe_id,
            character_template_id=template.id,
            template_version=template.persona_version,
            origin="preset",
            suggested_display_name=suggested_display_name,
            naming_version=naming_version,
            conn=self._required_conn(),
        )
        for candidate in self.list_candidates(universe_id, (str(row["status"]),)):
            if candidate.resident_id == row["id"]:
                return candidate
        raise RuntimeError("candidate details were not found")

    def list_candidates(
        self, universe_id: str, statuses: Sequence[str]
    ) -> Sequence[CandidateRecord]:
        """按 universe 和状态返回非 legacy 候选。"""

        return tuple(
            _candidate(row)
            for row in world_db.list_candidate_residents(
                universe_id=universe_id,
                statuses=statuses,
                conn=self._required_conn(),
            )
        )

    def count_active_residents(self, universe_id: str) -> int:
        """返回 active 居民数量。"""

        return world_db.count_active_residents(
            universe_id=universe_id,
            conn=self._required_conn(),
        )

    def activate_candidate_with_runtime(
        self,
        candidate: CandidateRecord,
        *,
        owner_platform_user_id: str,
        display_name: str,
    ) -> ResidentRecord:
        """在同一事务创建鸣蝉账号、激活居民并建立 conversation。"""

        self._require_owner_membership(owner_platform_user_id)
        conn = self._required_conn()
        world = world_db.get_universe(
            universe_id=candidate.universe_id,
            expected_app_id=MINGCHAN_APP_ID,
            conn=conn,
        )
        if world is None or str(world["owner_platform_user_id"]) != str(
            owner_platform_user_id
        ):
            raise MingchanWorldError("unauthorized")

        soul, identity, system_prompt = _persona_parts(
            candidate.template.persona_seed_json
        )
        runtime = insert_mingchan_resident_runtime_account(
            platform_user_id=owner_platform_user_id,
            display_name=display_name,
            system_prompt=system_prompt,
            soul_seed=soul,
            identity_seed=identity,
            registry=self._registry,
            conn=conn,
        )
        account = runtime["account"]
        if account.get("app_id") != MINGCHAN_APP_ID:
            raise RuntimeError("resident runtime account product mismatch")
        account_id = str(account["id"])

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
            owner_platform_user_id,
            ("active", "offline"),
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
        """在居民激活事务里幂等写入鸣蝉欢迎消息、Feed 和发布 outbox。"""

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
        """确认完成后关闭未选择候选。"""

        world_db.dismiss_unselected_candidate_residents(
            universe_id=universe_id,
            selected_template_ids=selected_template_ids,
            conn=self._required_conn(),
        )

    def list_residents_for_owner(
        self, platform_user_id: str, statuses: Sequence[str]
    ) -> Sequence[ResidentRecord]:
        """列出 owner 居民，并拒绝任何非鸣蝉 runtime account。"""

        self._require_owner_membership(platform_user_id)
        conn = self._required_conn()
        rows = world_db.list_resident_details_for_owner(
            owner_platform_user_id=platform_user_id,
            statuses=statuses,
            conn=conn,
        )
        residents = []
        for row in rows:
            account = conn.execute(
                "SELECT app_id FROM accounts WHERE id = ?",
                (row.get("runtime_account_id"),),
            ).fetchone()
            if account is None or str(account["app_id"]) != MINGCHAN_APP_ID:
                raise MingchanWorldError("resident_product_mismatch")
            residents.append(_resident(row))
        return tuple(residents)


__all__ = ["SqlMingchanWorldOnboardingRepository"]
