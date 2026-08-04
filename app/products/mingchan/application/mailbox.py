"""M4 mailbox platform service：确定性投递、request-time expiry 与 owner API。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import (
    count_active_residents,
    count_unread_character_letters,
    create_ai_conversation,
    create_character_letter_catalog_entry,
    create_resident,
    expire_due_character_letters,
    get_accepted_character_letter_resident,
    get_character_letter_for_owner,
    has_nonlegacy_resident_for_template,
    insert_resident_runtime_account,
    insert_character_letter,
    list_character_letter_catalog,
    list_character_letters_for_owner,
    list_mailbox_delivery_worlds,
    lock_character_letter_accept_scope,
    mark_locked_character_letter_accepted,
    mark_locked_character_letter_expired,
    prepare_character_letter_delivery,
    retire_character_letter_catalog_entry,
    transition_open_character_letter,
)
from app.db._backend import is_postgres
from app.db._core import connect
from app.products.mingchan.domain.companion_world.mailbox import (
    CharacterLetterRecord,
    MailboxPolicy,
    letter_delivery_fingerprint,
)
from app.time_utils import parse_db_timestamp

MAILBOX_POLICY_FAMILY = "companion_world_mailbox_v1"


class MailboxError(Exception):
    """Owner/admin mailbox 的稳定业务错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_mailbox_policy(config: Any = settings) -> MailboxPolicy:
    """从配置构造有界 mailbox policy；active `<8` 是不可配置产品常量。"""
    cooldown = int(config.mingchan_mailbox_delivery_cooldown_days)
    ttl = int(config.mingchan_mailbox_letter_ttl_days)
    if not 1 <= cooldown <= 365:
        raise ValueError("mailbox delivery cooldown must be between 1 and 365 days")
    if not 1 <= ttl <= 365:
        raise ValueError("mailbox letter ttl must be between 1 and 365 days")
    return MailboxPolicy(
        version=f"{MAILBOX_POLICY_FAMILY}:c{cooldown}:t{ttl}:a8",
        delivery_cooldown_days=cooldown,
        letter_ttl_days=ttl,
        delivery_active_limit=8,
    )


def _db_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _tags(raw: object) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _letter_record(row: Mapping[str, Any]) -> CharacterLetterRecord:
    """把 owner-scoped DB row 转成领域快照；公开 router 再做字段白名单。"""
    return CharacterLetterRecord(
        id=str(row["id"]),
        owner_platform_user_id=str(row["owner_platform_user_id"]),
        universe_id=str(row["universe_id"]),
        catalog_id=str(row["catalog_id"]),
        character_key=str(row["character_key"]),
        character_template_id=str(row["character_template_id"]),
        template_version=str(row["template_version"]),
        body_text=str(row["body_text"]),
        status=str(row["status"]),
        idempotency_key=str(row["idempotency_key"]),
        request_fingerprint=str(row["request_fingerprint"]),
        eligibility_snapshot=dict(row.get("eligibility_snapshot") or {}),
        policy_version=str(row["policy_version"]),
        delivered_at=str(row["delivered_at"]),
        expires_at=str(row["expires_at"]),
        accepted_resident_id=row.get("accepted_resident_id"),
        read_at=row.get("read_at"),
        deferred_at=row.get("deferred_at"),
        handled_at=row.get("handled_at"),
        character_name=row.get("character_name"),
        avatar_ref=row.get("avatar_ref"),
        summary=row.get("summary"),
        tags=_tags(row.get("tags_json")),
        source=str(row.get("source") or "organic"),
        wish_id=row.get("wish_id"),
    )


def _persona_parts(raw: Optional[str]) -> tuple[str, str, str]:
    """解析受控 persona seed；只接受 runtime 原语支持的固定字段。"""
    if not raw:
        return "", "", ""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as err:
        raise ValueError("invalid persona_seed_json") from err
    if not isinstance(value, dict):
        raise ValueError("invalid persona_seed_json")
    parts = (
        value.get("SOUL.md", value.get("soul", "")),
        value.get("IDENTITY.md", value.get("identity", "")),
        value.get("system_prompt", ""),
    )
    if not all(isinstance(item, str) for item in parts):
        raise ValueError("invalid persona_seed_json")
    return tuple(item.strip() for item in parts)  # type: ignore[return-value]


class CompanionWorldMailboxService:
    """编排 mailbox delivery/expiry 与 owner 隔离状态动作。"""

    def __init__(
        self,
        *,
        policy: Optional[MailboxPolicy] = None,
        registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    ) -> None:
        self.policy = policy or build_mailbox_policy()
        self.registry = registry

    def maintain_batch(
        self,
        *,
        now: datetime,
        after_universe_id: Optional[str],
        batch_size: int,
    ) -> Dict[str, Any]:
        """先批量过期，再按 world cursor 尝试投递；每个 world 内重算全部资格。"""
        clean_batch = max(1, min(int(batch_size), 500))
        now_text = _db_time(now)
        metrics = {
            "world_scanned": 0,
            "eligible": 0,
            "delivered": 0,
            "blocked_open": 0,
            "blocked_cooldown": 0,
            "blocked_capacity": 0,
            "catalog_empty": 0,
            "world_ineligible": 0,
            "expired": expire_due_character_letters(
                now=now_text,
                limit=clean_batch,
            ),
        }
        worlds = list_mailbox_delivery_worlds(
            after_universe_id=after_universe_id,
            limit=clean_batch,
        )
        results: list[Dict[str, str]] = []
        for world in worlds:
            metrics["world_scanned"] += 1
            result = self._deliver_world(
                universe_id=str(world["universe_id"]), now=now
            )
            metrics["expired"] += int(result.pop("_expired", 0))
            status = str(result["status"])
            if status in metrics:
                metrics[status] += 1
            if status == "delivered":
                metrics["eligible"] += 1
            results.append(result)
        return {
            "metrics": metrics,
            "results": results,
            "next_after_universe_id": (
                str(worlds[-1]["universe_id"])
                if len(worlds) >= clean_batch
                else None
            ),
        }

    def _deliver_world(self, *, universe_id: str, now: datetime) -> Dict[str, Any]:
        now_text = _db_time(now)
        cooldown_since = _db_time(
            now - timedelta(days=self.policy.delivery_cooldown_days)
        )
        expires_at = _db_time(now + timedelta(days=self.policy.letter_ttl_days))
        with connect() as tx:
            if not is_postgres():
                tx.execute("BEGIN IMMEDIATE")
            prepared = prepare_character_letter_delivery(
                universe_id=universe_id,
                now=now_text,
                cooldown_since=cooldown_since,
                active_limit=self.policy.delivery_active_limit,
                conn=tx,
            )
            if prepared["status"] != "eligible":
                return {
                    "universe_id": universe_id,
                    "status": str(prepared["status"]),
                    "_expired": int(prepared.get("expired") or 0),
                }
            catalog = dict(prepared["catalog"])
            snapshot = {
                "active_count": int(prepared["active_count"]),
                "active_limit": self.policy.delivery_active_limit,
                "catalog_version": str(catalog["template_version"]),
                "policy_version": self.policy.version,
            }
            fingerprint = letter_delivery_fingerprint(
                universe_id=universe_id,
                catalog_id=str(catalog["id"]),
                character_key=str(catalog["character_key"]),
                template_version=str(catalog["template_version"]),
                delivered_at=now_text,
                expires_at=expires_at,
                policy_version=self.policy.version,
                eligibility_snapshot=snapshot,
            )
            letter, _created = insert_character_letter(
                owner_platform_user_id=str(prepared["owner_platform_user_id"]),
                universe_id=universe_id,
                catalog_id=str(catalog["id"]),
                idempotency_key=(
                    f"mailbox-delivery:v1:{universe_id}:{catalog['character_key']}"
                ),
                request_fingerprint=fingerprint,
                eligibility_snapshot=snapshot,
                policy_version=self.policy.version,
                delivered_at=now_text,
                expires_at=expires_at,
                conn=tx,
            )
            return {
                "universe_id": universe_id,
                "letter_id": str(letter["id"]),
                "status": "delivered",
                "_expired": int(prepared.get("expired") or 0),
            }

    @staticmethod
    def _expire_owner(*, platform_user_id: str, now: str) -> None:
        expire_due_character_letters(
            owner_platform_user_id=platform_user_id,
            now=now,
        )

    def list_letters(
        self,
        platform_user_id: str,
        *,
        now: str,
        statuses: Optional[Sequence[str]],
        cursor_delivered_at: Optional[str],
        cursor_letter_id: Optional[str],
        limit: int,
    ) -> tuple[CharacterLetterRecord, ...]:
        """request-time 过期后按 owner/cursor 返回历史；读取不自动标记 read。"""
        self._expire_owner(platform_user_id=platform_user_id, now=now)
        try:
            rows = list_character_letters_for_owner(
                owner_platform_user_id=platform_user_id,
                statuses=statuses,
                cursor_delivered_at=cursor_delivered_at,
                cursor_letter_id=cursor_letter_id,
                limit=limit,
            )
        except ValueError as err:
            raise MailboxError("invalid_request") from err
        return tuple(_letter_record(row) for row in rows)

    def get_letter(
        self, platform_user_id: str, *, letter_id: str, now: str
    ) -> CharacterLetterRecord:
        """owner-scoped detail；跨 owner 与不存在统一 letter_not_found。"""
        self._expire_owner(platform_user_id=platform_user_id, now=now)
        row = get_character_letter_for_owner(
            letter_id=letter_id,
            owner_platform_user_id=platform_user_id,
        )
        if row is None:
            raise MailboxError("letter_not_found")
        return _letter_record(row)

    def count_unread(self, platform_user_id: str, *, now: str) -> int:
        """返回 request-time expiry 后的未读数。"""
        self._expire_owner(platform_user_id=platform_user_id, now=now)
        return count_unread_character_letters(
            owner_platform_user_id=platform_user_id,
            now=now,
        )

    def transition(
        self,
        platform_user_id: str,
        *,
        letter_id: str,
        target_status: str,
        now: str,
    ) -> CharacterLetterRecord:
        """执行 read/defer/decline；终态重放幂等，其他终态返回 letter_not_open。"""
        self._expire_owner(platform_user_id=platform_user_id, now=now)
        current = get_character_letter_for_owner(
            letter_id=letter_id,
            owner_platform_user_id=platform_user_id,
        )
        if current is None:
            raise MailboxError("letter_not_found")
        if current["status"] == target_status:
            return _letter_record(current)
        if current["status"] not in {"unread", "read", "deferred"}:
            raise MailboxError("letter_not_open")
        updated = transition_open_character_letter(
            letter_id=letter_id,
            owner_platform_user_id=platform_user_id,
            new_status=target_status,
            now=now,
            terminal_reason=("owner_declined" if target_status == "declined" else None),
        )
        if updated is None:
            raise MailboxError("letter_not_open")
        projected = get_character_letter_for_owner(
            letter_id=letter_id,
            owner_platform_user_id=platform_user_id,
        )
        if projected is None:
            raise RuntimeError("mailbox letter disappeared after transition")
        return _letter_record(projected)

    def accept_letter(
        self, platform_user_id: str, *, letter_id: str, now: str
    ) -> Dict[str, Any]:
        """单事务接受来信；重放返回同一 resident，不创建 binding/grant/通知。"""
        expired = False
        result: Optional[Dict[str, Any]] = None
        with connect() as tx:
            if not is_postgres():
                tx.execute("BEGIN IMMEDIATE")
            letter = lock_character_letter_accept_scope(
                letter_id=letter_id,
                owner_platform_user_id=platform_user_id,
                conn=tx,
            )
            if letter is None:
                raise MailboxError("letter_not_found")

            status = str(letter["status"])
            if status == "accepted":
                resident = get_accepted_character_letter_resident(
                    letter_id=letter_id,
                    owner_platform_user_id=platform_user_id,
                    conn=tx,
                )
                if resident is None:
                    raise RuntimeError("accepted mailbox letter is missing resident")
                result = {
                    "letter": _letter_record(letter),
                    "resident": resident,
                    "replayed": True,
                }
            elif status == "expired":
                expired = True
            elif status in {"unread", "read", "deferred"} and now >= str(
                letter["expires_at"]
            ):
                if not mark_locked_character_letter_expired(
                    letter_id=letter_id,
                    owner_platform_user_id=platform_user_id,
                    now=now,
                    conn=tx,
                ):
                    raise RuntimeError("locked mailbox letter expiry CAS failed")
                expired = True
            elif status not in {"unread", "read", "deferred"}:
                raise MailboxError("letter_not_open")
            else:
                common_available = (
                    letter["catalog_status"] == "active"
                    and letter["template_status"] == "active"
                    and str(letter["catalog_character_key"])
                    == str(letter["character_key"])
                    and str(letter["catalog_character_template_id"])
                    == str(letter["character_template_id"])
                    and str(letter["catalog_template_version"])
                    == str(letter["template_version"])
                    and str(letter["current_template_version"])
                    == str(letter["template_version"])
                )
                organic_available = (
                    str(letter.get("source") or "organic") == "organic"
                    and str(letter.get("catalog_source") or "organic") == "organic"
                    and letter["template_source_type"] in {"official", "operations"}
                )
                wish_available = (
                    letter.get("source") == "wish"
                    and letter.get("catalog_source") == "wish"
                    and letter["template_source_type"] == "generated"
                    and letter.get("template_owner_platform_user_id")
                    == platform_user_id
                )
                available = common_available and (organic_available or wish_available)
                if not available or has_nonlegacy_resident_for_template(
                    universe_id=str(letter["universe_id"]),
                    character_template_id=str(letter["character_template_id"]),
                    conn=tx,
                ):
                    raise MailboxError("letter_template_unavailable")
                if count_active_residents(
                    universe_id=str(letter["universe_id"]), conn=tx
                ) >= 10:
                    raise MailboxError("resident_capacity_exceeded")
                try:
                    soul, identity, system_prompt = _persona_parts(
                        letter.get("persona_seed_json")
                    )
                except ValueError as err:
                    raise MailboxError("letter_template_unavailable") from err
                runtime = insert_resident_runtime_account(
                    platform_user_id=platform_user_id,
                    display_name=str(letter["character_name"]),
                    system_prompt=system_prompt,
                    soul_seed=soul,
                    identity_seed=identity,
                    app_id=MINGCHAN_APP_ID,
                    registry=self.registry,
                    conn=tx,
                )
                account_id = str(runtime["account"]["id"])
                resident = create_resident(
                    universe_id=str(letter["universe_id"]),
                    character_template_id=str(letter["character_template_id"]),
                    template_version=str(letter["template_version"]),
                    origin="mailbox",
                    status="active",
                    runtime_account_id=account_id,
                    joined_at=now,
                    conn=tx,
                )
                conversation = create_ai_conversation(
                    universe_id=str(letter["universe_id"]),
                    resident_id=str(resident["id"]),
                    owner_platform_user_id=platform_user_id,
                    runtime_account_id=account_id,
                    conn=tx,
                )
                if not mark_locked_character_letter_accepted(
                    letter_id=letter_id,
                    owner_platform_user_id=platform_user_id,
                    resident_id=str(resident["id"]),
                    now=now,
                    conn=tx,
                ):
                    raise RuntimeError("locked mailbox letter accept CAS failed")
                accepted_letter = get_character_letter_for_owner(
                    letter_id=letter_id,
                    owner_platform_user_id=platform_user_id,
                    conn=tx,
                )
                if accepted_letter is None:
                    raise RuntimeError("mailbox letter disappeared after accept")
                result = {
                    "letter": _letter_record(accepted_letter),
                    "resident": {
                        "resident_id": str(resident["id"]),
                        "name": str(letter["character_name"]),
                        "avatar_ref": letter.get("avatar_ref"),
                        "origin": "mailbox",
                        "status": "active",
                        "conversation_id": str(conversation["id"]),
                        "conversation_state": str(conversation["state"]),
                    },
                    "replayed": False,
                }
        if expired:
            raise MailboxError("letter_expired")
        if result is None:
            raise RuntimeError("mailbox accept produced no result")
        return result


def create_mailbox_catalog_entry(
    *,
    character_key: str,
    character_template_id: str,
    template_version: str,
    letter_body: str,
    priority: int,
    created_by: str,
    available_from: Optional[str],
    available_until: Optional[str],
) -> tuple[Dict[str, Any], bool]:
    """校验 admin 输入并创建当前 policy 下的不可变 catalog entry。"""
    clean_body = str(letter_body or "").strip()
    if not clean_body or len(clean_body) > 2000:
        raise MailboxError("mailbox_catalog_invalid")
    if not -1000 <= int(priority) <= 1000:
        raise MailboxError("mailbox_catalog_invalid")
    start = parse_db_timestamp(available_from)
    end = parse_db_timestamp(available_until)
    if available_from and start is None:
        raise MailboxError("mailbox_catalog_invalid")
    if available_until and end is None:
        raise MailboxError("mailbox_catalog_invalid")
    if start is not None and end is not None and start >= end:
        raise MailboxError("mailbox_catalog_invalid")
    try:
        return create_character_letter_catalog_entry(
            character_key=character_key,
            character_template_id=character_template_id,
            template_version=template_version,
            letter_body=clean_body,
            policy_version=build_mailbox_policy().version,
            priority=priority,
            created_by=created_by,
            available_from=available_from,
            available_until=available_until,
        )
    except ValueError as err:
        raise MailboxError("mailbox_catalog_invalid") from err


def list_mailbox_catalog(
    *, statuses: Sequence[str], limit: int
) -> list[Dict[str, Any]]:
    """读取 admin catalog；非法过滤条件返回稳定错误。"""
    try:
        return list_character_letter_catalog(statuses=statuses, limit=limit)
    except ValueError as err:
        raise MailboxError("mailbox_catalog_invalid") from err


def retire_mailbox_catalog_entry(
    *, catalog_id: str, retired_by: str, retired_at: str
) -> Dict[str, Any]:
    """幂等 retire；不存在返回 catalog_not_found。"""
    row = retire_character_letter_catalog_entry(
        catalog_id=catalog_id,
        retired_by=retired_by,
        retired_at=retired_at,
    )
    if row is None:
        raise MailboxError("mailbox_catalog_not_found")
    return row


__all__ = [
    "CompanionWorldMailboxService",
    "MAILBOX_POLICY_FAMILY",
    "MailboxError",
    "build_mailbox_policy",
    "create_mailbox_catalog_entry",
    "list_mailbox_catalog",
    "retire_mailbox_catalog_entry",
]
