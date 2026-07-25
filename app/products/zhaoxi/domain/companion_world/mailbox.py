"""Companion World M4 私密 mailbox 纯领域 DTO、状态机与 repository port。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Tuple

LETTER_OPEN_STATUSES = frozenset({"unread", "read", "deferred"})
LETTER_TERMINAL_STATUSES = frozenset({"accepted", "declined", "expired"})


@dataclass(frozen=True)
class MailboxPolicy:
    """投递时钉住的 mailbox 策略；容量阈值是不可漂移产品常量。"""

    version: str
    delivery_cooldown_days: int = 30
    letter_ttl_days: int = 30
    delivery_active_limit: int = 8


@dataclass(frozen=True)
class LetterCatalogRecord:
    """一个不可变的运营来信角色版本。"""

    id: str
    character_key: str
    character_template_id: str
    template_version: str
    letter_body: str
    policy_version: str
    priority: int
    status: str
    available_from: Optional[str] = None
    available_until: Optional[str] = None


@dataclass(frozen=True)
class CharacterLetterRecord:
    """一个 owner/universe 隔离的私人来信快照。"""

    id: str
    owner_platform_user_id: str
    universe_id: str
    catalog_id: str
    character_key: str
    character_template_id: str
    template_version: str
    body_text: str
    status: str
    idempotency_key: str
    request_fingerprint: str
    eligibility_snapshot: Mapping[str, object]
    policy_version: str
    delivered_at: str
    expires_at: str
    accepted_resident_id: Optional[str] = None
    read_at: Optional[str] = None
    deferred_at: Optional[str] = None
    handled_at: Optional[str] = None
    character_name: Optional[str] = None
    avatar_ref: Optional[str] = None
    summary: Optional[str] = None
    tags: Tuple[str, ...] = ()


def letter_is_open(status: str) -> bool:
    """返回 letter 是否仍占用每世界唯一 open 名额。"""
    return status in LETTER_OPEN_STATUSES


def letter_transition_allowed(current_status: str, target_status: str) -> bool:
    """返回非接受动作是否符合冻结的 mailbox 状态机。"""
    if current_status not in LETTER_OPEN_STATUSES:
        return current_status == target_status
    if target_status == "read":
        return current_status in {"unread", "read"}
    return target_status in {"deferred", "declined", "expired", "accepted"}


def letter_delivery_fingerprint(
    *,
    universe_id: str,
    catalog_id: str,
    character_key: str,
    template_version: str,
    delivered_at: str,
    expires_at: str,
    policy_version: str,
    eligibility_snapshot: Mapping[str, object],
) -> str:
    """生成稳定 delivery fingerprint，保护 scheduler 幂等重放。"""
    canonical = json.dumps(
        {
            "universe_id": universe_id,
            "catalog_id": catalog_id,
            "character_key": character_key,
            "template_version": template_version,
            "delivered_at": delivered_at,
            "expires_at": expires_at,
            "policy_version": policy_version,
            "eligibility_snapshot": dict(eligibility_snapshot),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MailboxRepository(Protocol):
    """M4 mailbox 持久化端口；owner 隔离由 adapter 强制。"""

    def list_catalog(
        self, *, statuses: Sequence[str], limit: int
    ) -> Sequence[LetterCatalogRecord]: ...

    def deliver_letter(
        self, *, letter: CharacterLetterRecord
    ) -> tuple[CharacterLetterRecord, bool]: ...

    def get_letter_for_owner(
        self, *, letter_id: str, platform_user_id: str
    ) -> Optional[CharacterLetterRecord]: ...

    def list_letters_for_owner(
        self,
        *,
        platform_user_id: str,
        statuses: Optional[Sequence[str]],
        cursor_delivered_at: Optional[str],
        cursor_letter_id: Optional[str],
        limit: int,
    ) -> Sequence[CharacterLetterRecord]: ...

    def count_unread(self, *, platform_user_id: str, now: str) -> int: ...

    def transition_open_letter(
        self,
        *,
        letter_id: str,
        platform_user_id: str,
        new_status: str,
        now: str,
        terminal_reason: Optional[str],
    ) -> Optional[CharacterLetterRecord]: ...


__all__ = [
    "LETTER_OPEN_STATUSES",
    "LETTER_TERMINAL_STATUSES",
    "CharacterLetterRecord",
    "LetterCatalogRecord",
    "MailboxPolicy",
    "MailboxRepository",
    "letter_delivery_fingerprint",
    "letter_is_open",
    "letter_transition_allowed",
]
