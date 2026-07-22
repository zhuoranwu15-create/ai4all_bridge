"""Companion World App inbox SQL adapter 与 post-policy typed intent 接缝。"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from app.config import settings
from app.db import companion_world as world_db
from app.db import notifications as notification_db
from app.domains.companion_world import AppNotificationRecord
from app.domains.companion_world.proactive import (
    decide_human_proactive_delivery,
    is_human_proactive_category,
)
from app.platform.companion_world_repository import resolve_human_proactive_scope
from app.time_utils import BEIJING_TZ


def _notification(row: Dict[str, Any]) -> AppNotificationRecord:
    """把 owner-scoped projection 转为不泄漏内部字段的领域 DTO。"""
    return AppNotificationRecord(
        id=str(row["id"]),
        platform_user_id=str(row["platform_user_id"]),
        universe_id=str(row["universe_id"]),
        scope=str(row["scope"]),
        category=str(row["category"]),
        source_type=str(row["source_type"]),
        delivery_status=str(row["delivery_status"]),
        resident_id=row.get("resident_id"),
        resident_name=row.get("resident_name"),
        resident_avatar_ref=row.get("resident_avatar_ref"),
        title=row.get("title"),
        body_text=row.get("body_text"),
        target_type=str(row.get("target_type") or "none"),
        target_id=row.get("target_id"),
        delivered_at=row.get("delivered_at"),
        read_at=row.get("read_at"),
        expires_at=row.get("expires_at"),
    )


@dataclass(frozen=True)
class AppInboxIntent:
    """已通过现有 policy/moderation 的 per-resident App 通知意图。"""

    runtime_account_id: str
    category: str
    source_type: str
    idempotency_key: str
    body_text: str
    source_id: Optional[str] = None
    title: Optional[str] = None
    target_type: str = "none"
    target_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HumanAppInboxIntent:
    """真人级 App-only 通知意图；source_dedupe_key 只接受服务端稳定字段。"""

    runtime_account_id: str
    category: str
    source_type: str
    source_dedupe_key: str
    body_text: str
    source_id: Optional[str] = None
    title: Optional[str] = None
    target_type: str = "conversation"
    speaker_bound: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HumanAppInboxClaim:
    """隐藏 reservation 的安全完成凭据；token 不进入 API DTO。"""

    notification_id: str
    platform_user_id: str
    universe_id: str
    expected_resident_id: str
    expected_conversation_id: str
    runtime_account_ids: Tuple[str, ...]
    claim_token: str
    delivery_status: str


def app_inbox_fingerprint(
    *,
    platform_user_id: str,
    universe_id: str,
    resident_id: str,
    intent: AppInboxIntent,
) -> str:
    """按稳定 canonical JSON 计算 per-resident 通知 fingerprint。"""
    payload = json.dumps(
        {
            "v": 1,
            "platform_user_id": platform_user_id,
            "universe_id": universe_id,
            "resident_id": resident_id,
            "scope": "resident",
            "category": intent.category,
            "source_type": intent.source_type,
            "source_id": intent.source_id,
            "title": intent.title,
            "body_text": intent.body_text,
            "target_type": intent.target_type,
            "target_id": intent.target_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SqlAppNotificationRepository:
    """AppNotificationRepository 的 SQLite/PG adapter。"""

    def get_notification_for_owner(
        self, notification_id: str, platform_user_id: str
    ) -> Optional[AppNotificationRecord]:
        row = notification_db.get_app_notification_for_owner(
            notification_id=notification_id, platform_user_id=platform_user_id
        )
        return _notification(row) if row else None

    def list_visible_notifications(
        self,
        *,
        platform_user_id: str,
        now: str,
        unread_only: bool,
        cursor_delivered_at: Optional[str],
        cursor_notification_id: Optional[str],
        limit: int,
    ) -> Sequence[AppNotificationRecord]:
        return tuple(
            _notification(row)
            for row in notification_db.list_app_notifications(
                platform_user_id=platform_user_id,
                now=now,
                unread_only=unread_only,
                cursor_delivered_at=cursor_delivered_at,
                cursor_notification_id=cursor_notification_id,
                limit=limit,
            )
        )

    def count_unread(self, *, platform_user_id: str, now: str) -> int:
        return notification_db.count_unread_app_notifications(
            platform_user_id=platform_user_id, now=now
        )

    def mark_read(
        self,
        *,
        platform_user_id: str,
        notification_id: str,
        now: str,
        read_expires_at: str,
    ) -> Optional[AppNotificationRecord]:
        row = notification_db.mark_app_notification_read(
            notification_id=notification_id,
            platform_user_id=platform_user_id,
            now=now,
            read_expires_at=read_expires_at,
        )
        if row is None:
            return None
        return self.get_notification_for_owner(notification_id, platform_user_id)

    def mark_all_read(
        self, *, platform_user_id: str, now: str, read_expires_at: str
    ) -> Tuple[int, str]:
        return notification_db.mark_all_app_notifications_read(
            platform_user_id=platform_user_id,
            now=now,
            read_expires_at=read_expires_at,
        )

    def reserve_human_delivery(self, **kwargs: Any) -> Tuple[Optional[AppNotificationRecord], bool]:
        """实现领域端口的 hidden reservation 原语。"""

        row, acquired = notification_db.reserve_human_app_notification(**kwargs)
        return (_notification(row) if row else None), acquired

    def finalize_human_delivery(self, **kwargs: Any) -> Tuple[Optional[AppNotificationRecord], bool]:
        """实现领域端口的 reservation→visible CAS。"""

        row, finalized = notification_db.finalize_human_app_notification(**kwargs)
        if row is None:
            return None, finalized
        projected = notification_db.get_app_notification_for_owner(
            notification_id=str(row["id"]),
            platform_user_id=str(row["platform_user_id"]),
        )
        return (_notification(projected) if projected else None), finalized

    def cancel_human_delivery(self, **kwargs: Any) -> Tuple[Optional[AppNotificationRecord], bool]:
        """实现领域端口的 token-scoped cancel CAS。"""

        row, cancelled = notification_db.cancel_human_app_notification(**kwargs)
        return (_notification(row) if row else None), cancelled


class AppInboxAdapter:
    """把 post-policy per-resident intent 解析到 owner world 后写 visible inbox。"""

    def can_deliver(self, runtime_account_id: str) -> bool:
        """仅 default-off flag 开启且账号映射 active confirmed world 时可入箱。"""
        if not bool(getattr(settings, "companion_world_app_inbox_enabled", False)):
            return False
        scope = world_db.resolve_resident_memory_scope(
            runtime_account_id=runtime_account_id
        )
        if scope is None or scope.get("status") != "active":
            return False
        world = world_db.get_universe(universe_id=str(scope["universe_id"]))
        return bool(
            world
            and world.get("status") == "active"
            and world.get("onboarding_state") == "confirmed"
        )

    def deliver(
        self, intent: AppInboxIntent, *, now: datetime
    ) -> Tuple[AppNotificationRecord, bool]:
        """投递一条 visible 通知；form-A/offline/跨 owner 输入全部 fail-closed。"""
        if not bool(getattr(settings, "companion_world_app_inbox_enabled", False)):
            raise ValueError("app inbox disabled")
        clean_text = str(intent.body_text or "").strip()
        if not clean_text or len(clean_text) > 2000:
            raise ValueError("invalid app inbox body")
        scope = world_db.resolve_resident_memory_scope(
            runtime_account_id=intent.runtime_account_id
        )
        if scope is None or scope.get("status") != "active":
            raise ValueError("active world resident not found")
        world = world_db.get_universe(universe_id=str(scope["universe_id"]))
        if world is None:
            raise ValueError("world not found")
        current = (
            now.astimezone(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
            if now.tzinfo is not None
            else now.replace(microsecond=0)
        )
        delivered_at = current.strftime("%Y-%m-%d %H:%M:%S")
        expires_at = (current + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        resolved_intent = AppInboxIntent(
            runtime_account_id=intent.runtime_account_id,
            category=str(intent.category or "").strip(),
            source_type=str(intent.source_type or "").strip(),
            source_id=intent.source_id,
            idempotency_key=str(intent.idempotency_key or "").strip(),
            title=(str(intent.title).strip() if intent.title is not None else None),
            body_text=clean_text,
            target_type=intent.target_type,
            target_id=intent.target_id,
            metadata=intent.metadata,
        )
        fingerprint = app_inbox_fingerprint(
            platform_user_id=str(world["owner_platform_user_id"]),
            universe_id=str(scope["universe_id"]),
            resident_id=str(scope["resident_id"]),
            intent=resolved_intent,
        )
        row, created = notification_db.insert_visible_app_notification(
            platform_user_id=str(world["owner_platform_user_id"]),
            universe_id=str(scope["universe_id"]),
            resident_id=str(scope["resident_id"]),
            scope="resident",
            category=resolved_intent.category,
            source_type=resolved_intent.source_type,
            source_id=resolved_intent.source_id,
            idempotency_key=resolved_intent.idempotency_key,
            request_fingerprint=fingerprint,
            title=resolved_intent.title,
            body_text=resolved_intent.body_text,
            target_type=resolved_intent.target_type,
            target_id=resolved_intent.target_id,
            delivered_at=delivered_at,
            expires_at=expires_at,
            now=delivered_at,
            metadata=dict(resolved_intent.metadata),
        )
        projected = notification_db.get_app_notification_for_owner(
            notification_id=str(row["id"]),
            platform_user_id=str(world["owner_platform_user_id"]),
        )
        if projected is None:
            raise RuntimeError("visible notification disappeared")
        return _notification(projected), created

    def reserve_human(
        self,
        intent: HumanAppInboxIntent,
        *,
        now: datetime,
        include_observation: bool = False,
    ) -> Union[
        Tuple[Optional[HumanAppInboxClaim], bool],
        Tuple[Optional[HumanAppInboxClaim], bool, str],
    ]:
        """在出站写入前抢占真人级 24h App-only claim。"""

        def _result(
            claim: Optional[HumanAppInboxClaim], acquired: bool, reason: str
        ) -> Union[
            Tuple[Optional[HumanAppInboxClaim], bool],
            Tuple[Optional[HumanAppInboxClaim], bool, str],
        ]:
            if include_observation:
                return claim, acquired, reason
            return claim, acquired

        inbox_enabled = bool(
            getattr(settings, "companion_world_app_inbox_enabled", False)
        )
        human_enabled = bool(
            getattr(
                settings,
                "companion_world_app_only_human_proactive_enabled",
                False,
            )
        )
        if not inbox_enabled or not human_enabled:
            return _result(None, False, "flags_disabled")
        if not is_human_proactive_category(intent.category):
            raise ValueError("category is not human proactive")
        scope = resolve_human_proactive_scope(intent.runtime_account_id)
        if scope is None or scope.app_speaker is None:
            return _result(None, False, "speaker_unavailable")
        decision = decide_human_proactive_delivery(
            legacy_weixin_route_available=scope.legacy_weixin_route_available,
            app_inbox_enabled=inbox_enabled,
            app_only_human_enabled=human_enabled,
            has_app_speaker=True,
        )
        if (
            decision.mode != "app_inbox"
            or scope.app_speaker.runtime_account_id != intent.runtime_account_id
        ):
            return _result(None, False, "route_not_app_inbox")
        current = (
            now.astimezone(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
            if now.tzinfo is not None
            else now.replace(microsecond=0)
        )
        clean_text = str(intent.body_text or "").strip()
        if not clean_text or len(clean_text) > 2000:
            raise ValueError("invalid app inbox body")
        dedupe_payload = json.dumps(
            {
                "v": 1,
                "platform_user_id": scope.platform_user_id,
                "category": intent.category,
                "source_type": intent.source_type,
                "source_dedupe_key": str(intent.source_dedupe_key),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        dedupe_hash = hashlib.sha256(dedupe_payload.encode("utf-8")).hexdigest()
        idempotency_key = f"human-proactive:v1:{intent.category}:{dedupe_hash}"
        fingerprint_payload = json.dumps(
            {
                "v": 1,
                "platform_user_id": scope.platform_user_id,
                "universe_id": scope.universe_id,
                "category": intent.category,
                "source_type": intent.source_type,
                "source_id": intent.source_id,
                "title": intent.title,
                "body_text": clean_text,
                "target_type": intent.target_type,
                "speaker_bound": bool(intent.speaker_bound),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(
            fingerprint_payload.encode("utf-8")
        ).hexdigest()
        claim_token = uuid.uuid4().hex
        now_text = current.strftime("%Y-%m-%d %H:%M:%S")
        row, acquired, observation = notification_db.reserve_human_app_notification_observed(
            platform_user_id=scope.platform_user_id,
            universe_id=scope.universe_id,
            category=intent.category,
            source_type=intent.source_type,
            source_id=intent.source_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            claim_token=claim_token,
            claim_expires_at=(current + timedelta(minutes=5)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            now=now_text,
            visible_since=(current - timedelta(hours=24)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            metadata={**intent.metadata, "speaker_bound": bool(intent.speaker_bound)},
        )
        if row is None:
            return _result(None, False, observation)
        claim = HumanAppInboxClaim(
            notification_id=str(row["id"]),
            platform_user_id=scope.platform_user_id,
            universe_id=scope.universe_id,
            expected_resident_id=scope.app_speaker.resident_id,
            expected_conversation_id=scope.app_speaker.conversation_id,
            runtime_account_ids=scope.runtime_account_ids,
            claim_token=(
                claim_token if acquired else str(row.get("claim_token") or "")
            ),
            delivery_status=str(row["delivery_status"]),
        )
        return _result(claim, acquired, observation)

    def finalize_human(
        self,
        claim: HumanAppInboxClaim,
        intent: HumanAppInboxIntent,
        *,
        now: datetime,
    ) -> Tuple[Optional[AppNotificationRecord], bool]:
        """投递事务重选并锁 speaker；强绑定 speaker 失活会取消 reservation。"""

        current = (
            now.astimezone(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
            if now.tzinfo is not None
            else now.replace(microsecond=0)
        )
        now_text = current.strftime("%Y-%m-%d %H:%M:%S")
        row, finalized = notification_db.finalize_human_app_notification(
            notification_id=claim.notification_id,
            platform_user_id=claim.platform_user_id,
            universe_id=claim.universe_id,
            claim_token=claim.claim_token,
            expected_resident_id=claim.expected_resident_id,
            allow_speaker_reselection=not intent.speaker_bound,
            title=intent.title,
            body_text=intent.body_text,
            target_type=intent.target_type,
            target_id=(
                claim.expected_conversation_id
                if intent.target_type == "conversation"
                else None
            ),
            delivered_at=now_text,
            expires_at=(current + timedelta(days=30)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            now=now_text,
        )
        if row is None:
            return None, finalized
        projected = notification_db.get_app_notification_for_owner(
            notification_id=claim.notification_id,
            platform_user_id=claim.platform_user_id,
        )
        return (_notification(projected) if projected else None), finalized

    def cancel_human(
        self,
        claim: HumanAppInboxClaim,
        *,
        reason: str,
        now: datetime,
    ) -> bool:
        """按 token 取消 reservation；过期或旧 token 为幂等 no-op。"""

        current = (
            now.astimezone(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
            if now.tzinfo is not None
            else now.replace(microsecond=0)
        )
        _row, cancelled = notification_db.cancel_human_app_notification(
            notification_id=claim.notification_id,
            platform_user_id=claim.platform_user_id,
            claim_token=claim.claim_token,
            reason=reason,
            now=current.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(current + timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )
        return cancelled


__all__ = [
    "AppInboxAdapter",
    "AppInboxIntent",
    "HumanAppInboxClaim",
    "HumanAppInboxIntent",
    "SqlAppNotificationRepository",
    "app_inbox_fingerprint",
]
