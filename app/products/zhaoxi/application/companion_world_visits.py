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
    insert_platform_user_block,
    insert_universe_invite,
    list_due_universe_invites,
    list_due_universe_visits,
    list_open_universe_visits_between,
    list_published_feed_posts_for_owner,
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
from app.products.zhaoxi.domain.companion_world.visits import VisitPolicy
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

    def list_invites_current(
        self, platform_user_id: str, *, now: datetime
    ) -> Sequence[Dict[str, Any]]:
        """request-time 清理 owner 到期 invite 后返回当前状态。"""
        for invite in list_universe_invites_for_owner(
            owner_platform_user_id=platform_user_id,
            statuses=("active",),
            limit=100,
        ):
            if now >= parse_db_timestamp(invite["expires_at"]):
                self._expire_invite_candidate(invite, now=now)
        return self.list_invites(platform_user_id)

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

    def list_visits_current(
        self,
        platform_user_id: str,
        *,
        now: datetime,
        statuses: Optional[Sequence[str]] = None,
    ) -> Sequence[Dict[str, Any]]:
        """request-time 终结当前 participant 已到期 visits 后返回列表。"""
        rows = list_universe_visits_for_participant(
            platform_user_id=platform_user_id,
            statuses=("pending", "active"),
            limit=100,
        )
        for row in rows:
            due_at = row.get(
                "pending_expires_at" if row["status"] == "pending" else "expires_at"
            )
            if due_at and now >= parse_db_timestamp(due_at):
                self._expire_visit_candidate(row, now=now)
        return self.list_visits(platform_user_id, statuses=statuses)

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

    def list_feed(
        self,
        platform_user_id: str,
        *,
        visit_id: str,
        now: datetime,
        cursor_published_at: Optional[str],
        cursor_post_id: Optional[str],
        limit: int,
    ) -> Sequence[Dict[str, Any]]:
        """用 server-resolved active visit 读取 published Feed，绝不接受 world id。"""
        preview = get_universe_visit(visit_id=visit_id)
        if preview is None or preview["visitor_platform_user_id"] != platform_user_id:
            raise VisitError("visit_not_found")
        expired = False
        blocked = False
        rows: Sequence[Dict[str, Any]] = ()
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(platform_user_id=platform_user_id, conn=conn)
            lock_universe(universe_id=preview["universe_id"], conn=conn)
            locked = lock_universe_visit(visit_id=visit_id, conn=conn)
            if locked is None or locked["visitor_platform_user_id"] != platform_user_id:
                raise VisitError("visit_not_found")
            if locked["status"] != "active":
                raise VisitError("visit_not_active")
            if now >= parse_db_timestamp(locked["expires_at"]):
                mark_universe_visit_terminal(
                    visit_id=visit_id,
                    expected_status="active",
                    target_status="expired",
                    now=_db_time(now),
                    terminal_reason="active_expired",
                    conn=conn,
                )
                expired = True
            elif has_platform_user_block(
                first_platform_user_id=locked["owner_platform_user_id"],
                second_platform_user_id=locked["visitor_platform_user_id"],
                conn=conn,
            ):
                mark_universe_visit_terminal(
                    visit_id=visit_id,
                    expected_status="active",
                    target_status="blocked",
                    now=_db_time(now),
                    terminal_reason="contact_blocked",
                    conn=conn,
                )
                blocked = True
            else:
                try:
                    rows = tuple(
                        list_published_feed_posts_for_owner(
                            owner_platform_user_id=locked[
                                "owner_platform_user_id"
                            ],
                            cursor_published_at=cursor_published_at,
                            cursor_post_id=cursor_post_id,
                            limit=limit,
                            conn=conn,
                        )
                    )
                except ValueError as err:
                    if str(err) == "invalid_cursor":
                        raise VisitError("invalid_cursor") from err
                    raise VisitError("visit_not_active") from err
        if expired:
            raise VisitError("visit_not_active")
        if blocked:
            raise VisitError("visit_contact_blocked")
        return rows

    def block(
        self, platform_user_id: str, *, visit_id: str, now: datetime
    ) -> Dict[str, Any]:
        """当前 participant 拉黑对方，并终结双方任一方向全部 open visits。"""
        preview = get_universe_visit(visit_id=visit_id)
        if preview is None or platform_user_id not in {
            preview["owner_platform_user_id"],
            preview["visitor_platform_user_id"],
        }:
            raise VisitError("visit_not_found")
        counterpart = (
            preview["visitor_platform_user_id"]
            if preview["owner_platform_user_id"] == platform_user_id
            else preview["owner_platform_user_id"]
        )
        terminated = 0
        with connect() as conn:
            _write_begin(conn)
            for user_id in sorted({platform_user_id, counterpart}):
                lock_platform_user_for_visit(platform_user_id=user_id, conn=conn)
            open_visits = list_open_universe_visits_between(
                first_platform_user_id=platform_user_id,
                second_platform_user_id=counterpart,
                conn=conn,
            )
            for universe_id in sorted({row["universe_id"] for row in open_visits}):
                lock_universe(universe_id=universe_id, conn=conn)
            insert_platform_user_block(
                blocker_platform_user_id=platform_user_id,
                blocked_platform_user_id=counterpart,
                created_at=_db_time(now),
                conn=conn,
            )
            for row in open_visits:
                locked = lock_universe_visit(visit_id=row["id"], conn=conn)
                if locked is None or locked["status"] not in {"pending", "active"}:
                    continue
                mark_universe_visit_terminal(
                    visit_id=locked["id"],
                    expected_status=locked["status"],
                    target_status="blocked",
                    now=_db_time(now),
                    terminal_reason="contact_blocked",
                    conn=conn,
                )
                terminated += 1
        return {"blocked": True, "terminated_visits": terminated}

    def _expire_invite_candidate(
        self, candidate: Dict[str, Any], *, now: datetime
    ) -> bool:
        """按 owner→world 锁序重查并终结一个 due invite。"""
        changed = False
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(
                platform_user_id=candidate["owner_platform_user_id"], conn=conn
            )
            lock_universe(universe_id=candidate["universe_id"], conn=conn)
            locked = get_universe_invite_by_code_hash(
                code_hash=candidate["code_hash"], conn=conn, for_update=True
            )
            if (
                locked is not None
                and locked["status"] == "active"
                and now >= parse_db_timestamp(locked["expires_at"])
            ):
                mark_universe_invite_terminal(
                    invite_id=locked["id"],
                    owner_platform_user_id=locked["owner_platform_user_id"],
                    target_status="expired",
                    now=_db_time(now),
                    conn=conn,
                )
                changed = True
        return changed

    def _expire_visit_candidate(
        self, candidate: Dict[str, Any], *, now: datetime
    ) -> bool:
        """按 visitor→world→visit 锁序重查并终结一个 due visit。"""
        changed = False
        with connect() as conn:
            _write_begin(conn)
            lock_platform_user_for_visit(
                platform_user_id=candidate["visitor_platform_user_id"], conn=conn
            )
            lock_universe(universe_id=candidate["universe_id"], conn=conn)
            locked = lock_universe_visit(visit_id=candidate["id"], conn=conn)
            if locked is None or locked["status"] not in {"pending", "active"}:
                return False
            due_raw = locked[
                "pending_expires_at" if locked["status"] == "pending" else "expires_at"
            ]
            if due_raw and now >= parse_db_timestamp(due_raw):
                mark_universe_visit_terminal(
                    visit_id=locked["id"],
                    expected_status=locked["status"],
                    target_status="expired",
                    now=_db_time(now),
                    terminal_reason=(
                        "pending_expired"
                        if locked["status"] == "pending"
                        else "active_expired"
                    ),
                    conn=conn,
                )
                changed = True
        return changed

    def maintain_expiry_batch(
        self, *, now: datetime, batch_size: int
    ) -> Dict[str, Any]:
        """central scheduler 有界清理 invite/pending/active expiry。"""
        clean_limit = max(1, min(int(batch_size), 500))
        invite_candidates = list_due_universe_invites(
            now=_db_time(now), limit=clean_limit
        )
        remaining = max(0, clean_limit - len(invite_candidates))
        visit_candidates = (
            list_due_universe_visits(now=_db_time(now), limit=remaining)
            if remaining
            else []
        )
        expired_invites = sum(
            int(self._expire_invite_candidate(item, now=now))
            for item in invite_candidates
        )
        expired_visits = sum(
            int(self._expire_visit_candidate(item, now=now))
            for item in visit_candidates
        )
        return {
            "metrics": {
                "scanned": len(invite_candidates) + len(visit_candidates),
                "expired_invites": expired_invites,
                "expired_visits": expired_visits,
            }
        }


__all__ = ["CompanionWorldVisitService", "VisitError"]
