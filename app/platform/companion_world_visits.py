"""M5 invite/pending/active visit 强事务与稳定业务错误。"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Sequence

from app.db import (
    count_open_universe_visits_for_visitor,
    create_human_conversation_for_visit,
    get_human_conversation_for_visit,
    get_universe,
    get_universe_invite_by_code_hash,
    get_universe_invite_for_owner,
    get_universe_visit,
    has_platform_user_block,
    insert_pending_universe_visit,
    insert_universe_invite,
    list_universe_invites_for_owner,
    list_universe_visits_for_participant,
    lock_platform_user_for_visit,
    lock_universe,
    lock_universe_visit,
    mark_universe_invite_terminal,
    mark_universe_visit_active,
    mark_universe_visit_terminal,
)
from app.db._backend import IntegrityError, is_postgres
from app.db._core import connect
from app.domains.companion_world.visits import VisitPolicy
from app.time_utils import parse_db_timestamp

_INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class VisitError(Exception):
    """M5 owner/visitor API 的稳定业务错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _db_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _write_begin(conn) -> None:
    if not is_postgres():
        conn.execute("BEGIN IMMEDIATE")


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _require_home_world(platform_user_id: str) -> Dict[str, Any]:
    world = get_universe(owner_platform_user_id=platform_user_id)
    if (
        world is None
        or world["status"] != "active"
        or world["onboarding_state"] != "confirmed"
    ):
        raise VisitError("world_not_ready")
    return world


def _visit_public(row: Dict[str, Any], platform_user_id: str) -> Dict[str, Any]:
    """构造不暴露内部真人 id/universe id 的 participant 投影。"""
    is_owner = row["owner_platform_user_id"] == platform_user_id
    return {
        "visit_id": row["id"],
        "role": "owner" if is_owner else "visitor",
        "counterpart_display_name": row.get(
            "visitor_display_name" if is_owner else "owner_display_name"
        ),
        "status": row["status"],
        "pending_expires_at": row.get("pending_expires_at"),
        "accepted_at": row.get("accepted_at"),
        "expires_at": row.get("expires_at"),
        "terminal_at": row.get("terminal_at"),
        "terminal_reason": row.get("terminal_reason"),
        "created_at": row["created_at"],
    }


class CompanionWorldVisitService:
    """M5 invite/visit 最小服务；Feed 与聊天消息分别由后续批次接入。"""

    def __init__(self, policy: VisitPolicy = VisitPolicy()) -> None:
        self.policy = policy

    def create_invite(
        self, platform_user_id: str, *, now: datetime
    ) -> Dict[str, Any]:
        """为 owner home world 占用最小空 slot 并返回一次性明文 code。"""
        world = _require_home_world(platform_user_id)
        code = secrets.token_urlsafe(32)
        expires_at = now + timedelta(hours=self.policy.invite_ttl_hours)
        try:
            with connect() as conn:
                _write_begin(conn)
                lock_platform_user_for_visit(
                    platform_user_id=platform_user_id, conn=conn
                )
                locked_world = lock_universe(universe_id=world["id"], conn=conn)
                if (
                    locked_world is None
                    or locked_world["owner_platform_user_id"] != platform_user_id
                ):
                    raise VisitError("world_not_ready")
                invite = insert_universe_invite(
                    universe_id=world["id"],
                    owner_platform_user_id=platform_user_id,
                    code_hash=_code_hash(code),
                    code_prefix=code[:6],
                    expires_at=_db_time(expires_at),
                    created_at=_db_time(now),
                    conn=conn,
                )
        except ValueError as err:
            if str(err) == "world visit limit reached":
                raise VisitError("world_visit_limit_reached") from err
            raise
        return {"invite": invite, "code": code}

    def list_invites(self, platform_user_id: str) -> Sequence[Dict[str, Any]]:
        """列出 owner 自己的 invite；明文 code/hash 永不返回。"""
        return tuple(
            list_universe_invites_for_owner(
                owner_platform_user_id=platform_user_id, limit=100
            )
        )

    def revoke_invite(
        self, platform_user_id: str, *, invite_id: str, now: datetime
    ) -> Dict[str, Any]:
        """owner 撤销未兑换 invite 并释放 slot；重复撤销幂等。"""
        preview = get_universe_invite_for_owner(
            invite_id=invite_id, owner_platform_user_id=platform_user_id
        )
        if preview is None:
            raise VisitError("invite_not_found")
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(platform_user_id=platform_user_id, conn=conn)
            lock_universe(universe_id=preview["universe_id"], conn=conn)
            try:
                updated = mark_universe_invite_terminal(
                    invite_id=invite_id,
                    owner_platform_user_id=platform_user_id,
                    target_status="revoked",
                    now=_db_time(now),
                    conn=conn,
                )
            except ValueError as err:
                raise VisitError("invite_unavailable") from err
        if updated is None:
            raise VisitError("invite_not_found")
        return updated

    def redeem(
        self, platform_user_id: str, *, code: str, now: datetime
    ) -> Dict[str, Any]:
        """B 兑换 code 为 pending；锁 B 容量后原位转移 A slot。"""
        clean = str(code or "").strip()
        if not _INVITE_CODE_RE.fullmatch(clean):
            raise VisitError("invalid_invite_code")
        preview = get_universe_invite_by_code_hash(code_hash=_code_hash(clean))
        if preview is None:
            raise VisitError("invite_not_found")
        expired = False
        result: Optional[Dict[str, Any]] = None
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(platform_user_id=platform_user_id, conn=conn)
            locked_world = lock_universe(universe_id=preview["universe_id"], conn=conn)
            invite = get_universe_invite_by_code_hash(
                code_hash=_code_hash(clean), conn=conn, for_update=True
            )
            if invite is None:
                raise VisitError("invite_not_found")
            if locked_world is None or locked_world["owner_platform_user_id"] != invite[
                "owner_platform_user_id"
            ]:
                raise VisitError("invite_unavailable")
            if invite["status"] != "active":
                raise VisitError("invite_unavailable")
            if now >= parse_db_timestamp(invite["expires_at"]):
                mark_universe_invite_terminal(
                    invite_id=invite["id"],
                    owner_platform_user_id=invite["owner_platform_user_id"],
                    target_status="expired",
                    now=_db_time(now),
                    conn=conn,
                )
                expired = True
            else:
                owner_id = str(invite["owner_platform_user_id"])
                if owner_id == platform_user_id:
                    raise VisitError("self_invite_not_allowed")
                if has_platform_user_block(
                    first_platform_user_id=owner_id,
                    second_platform_user_id=platform_user_id,
                    conn=conn,
                ):
                    raise VisitError("visit_contact_blocked")
                if count_open_universe_visits_for_visitor(
                    visitor_platform_user_id=platform_user_id, conn=conn
                ) >= self.policy.visitor_open_limit:
                    raise VisitError("visitor_visit_limit_reached")
                try:
                    result = insert_pending_universe_visit(
                        invite_id=invite["id"],
                        universe_id=invite["universe_id"],
                        owner_platform_user_id=owner_id,
                        visitor_platform_user_id=platform_user_id,
                        pending_expires_at=_db_time(
                            now + timedelta(days=self.policy.pending_ttl_days)
                        ),
                        created_at=_db_time(now),
                        conn=conn,
                    )
                except IntegrityError as err:
                    raise VisitError("visit_already_open") from err
        if expired:
            raise VisitError("invite_expired")
        if result is None:
            raise RuntimeError("redeem completed without pending visit")
        return result

    def list_visits(
        self, platform_user_id: str, *, statuses: Optional[Sequence[str]] = None
    ) -> Sequence[Dict[str, Any]]:
        """列出当前真人作为 owner/visitor 的 visit 公开投影。"""
        rows = list_universe_visits_for_participant(
            platform_user_id=platform_user_id, statuses=statuses, limit=100
        )
        return tuple(_visit_public(row, platform_user_id) for row in rows)

    def accept(
        self, platform_user_id: str, *, visit_id: str, now: datetime
    ) -> Dict[str, Any]:
        """A 接受 pending；active + 30d expiry + human conversation 同事务。"""
        preview = get_universe_visit(visit_id=visit_id)
        if preview is None or preview["owner_platform_user_id"] != platform_user_id:
            raise VisitError("visit_not_found")
        expired = False
        replayed = False
        visit: Optional[Dict[str, Any]] = None
        conversation: Optional[Dict[str, Any]] = None
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(
                platform_user_id=preview["visitor_platform_user_id"], conn=conn
            )
            lock_universe(universe_id=preview["universe_id"], conn=conn)
            locked = lock_universe_visit(visit_id=visit_id, conn=conn)
            if locked is None or locked["owner_platform_user_id"] != platform_user_id:
                raise VisitError("visit_not_found")
            if locked["status"] == "active":
                replayed = True
                visit = locked
                conversation = get_human_conversation_for_visit(
                    visit_id=visit_id, conn=conn
                )
            elif locked["status"] != "pending":
                raise VisitError("visit_not_pending")
            elif now >= parse_db_timestamp(locked["pending_expires_at"]):
                visit = mark_universe_visit_terminal(
                    visit_id=visit_id,
                    expected_status="pending",
                    target_status="expired",
                    now=_db_time(now),
                    terminal_reason="pending_expired",
                    conn=conn,
                )
                expired = True
            elif has_platform_user_block(
                first_platform_user_id=locked["owner_platform_user_id"],
                second_platform_user_id=locked["visitor_platform_user_id"],
                conn=conn,
            ):
                raise VisitError("visit_contact_blocked")
            else:
                visit = mark_universe_visit_active(
                    visit_id=visit_id,
                    accepted_at=_db_time(now),
                    expires_at=_db_time(
                        now + timedelta(days=self.policy.active_ttl_days)
                    ),
                    conn=conn,
                )
                conversation = create_human_conversation_for_visit(
                    visit_id=visit_id,
                    owner_platform_user_id=visit["owner_platform_user_id"],
                    visitor_platform_user_id=visit["visitor_platform_user_id"],
                    created_at=_db_time(now),
                    conn=conn,
                )
        if expired:
            raise VisitError("visit_pending_expired")
        if visit is None or conversation is None:
            raise RuntimeError("visit accept missing conversation")
        return {"visit": visit, "conversation": conversation, "replayed": replayed}

    def terminate(
        self,
        platform_user_id: str,
        *,
        visit_id: str,
        action: str,
        now: datetime,
    ) -> Dict[str, Any]:
        """执行 reject/cancel/leave/revoke，按 action 强制角色与当前状态。"""
        rules = {
            "reject": ("owner", "pending", "rejected"),
            "cancel": ("visitor", "pending", "cancelled"),
            "leave": ("visitor", "active", "left"),
            "revoke": ("owner", "active", "revoked"),
        }
        if action not in rules:
            raise ValueError("invalid visit terminal action")
        preview = get_universe_visit(visit_id=visit_id)
        role, expected, target = rules[action]
        actor_field = (
            "owner_platform_user_id" if role == "owner" else "visitor_platform_user_id"
        )
        if preview is None or preview[actor_field] != platform_user_id:
            raise VisitError("visit_not_found")
        if preview["status"] == target:
            return preview
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(
                platform_user_id=preview["visitor_platform_user_id"], conn=conn
            )
            lock_universe(universe_id=preview["universe_id"], conn=conn)
            locked = lock_universe_visit(visit_id=visit_id, conn=conn)
            if locked is None or locked[actor_field] != platform_user_id:
                raise VisitError("visit_not_found")
            if locked["status"] == target:
                return locked
            if locked["status"] != expected:
                raise VisitError(
                    "visit_not_pending" if expected == "pending" else "visit_not_active"
                )
            visit = mark_universe_visit_terminal(
                visit_id=visit_id,
                expected_status=expected,
                target_status=target,
                now=_db_time(now),
                terminal_reason=f"{role}_{action}",
                conn=conn,
            )
        return visit


__all__ = ["CompanionWorldVisitService", "VisitError"]
