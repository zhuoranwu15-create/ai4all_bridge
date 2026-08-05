"""Companion World M4 lifecycle 纯领域 DTO、状态机与 repository port。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional, Protocol, Sequence, Tuple

LIFECYCLE_EVENT_TYPES = frozenset(
    {"inactivity", "value_misalignment", "severe_abuse"}
)
LIFECYCLE_EVENT_STATUSES = frozenset(
    {"cooling_down", "review_pending", "committed", "cancelled", "rejected"}
)
LIFECYCLE_CRISIS_CATEGORIES = frozenset(
    {"crisis", "self_harm", "high_vulnerability"}
)
LIFECYCLE_SEVERE_ABUSE_CATEGORIES = frozenset(
    {"severe_abuse", "credible_threat", "hate_harassment"}
)

_TRANSITIONS = {
    "cooling_down": frozenset({"review_pending", "cancelled", "rejected"}),
    "review_pending": frozenset({"committed", "cancelled", "rejected"}),
}


@dataclass(frozen=True)
class LifecyclePolicy:
    """一次 lifecycle 评估使用的冻结策略快照。"""

    version: str
    inactivity_days: int = 60
    evidence_window_days: int = 30
    mismatch_min_events: int = 3
    mismatch_min_span_days: int = 14
    cooldown_days: int = 7
    crisis_freeze_days: int = 30


@dataclass(frozen=True)
class LifecycleEvidenceRef:
    """不含原文的结构化 lifecycle 证据引用。"""

    source_type: str
    source_id: str
    category: str
    observed_at: datetime
    confidence: Optional[float] = None
    policy_version: Optional[str] = None

    def as_mapping(self) -> Mapping[str, object]:
        """转换成可持久化的安全引用，不携带消息正文或账号标识。"""
        result: dict[str, object] = {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "category": self.category,
            "observed_at": self.observed_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.confidence is not None:
            result["confidence"] = float(self.confidence)
        if self.policy_version:
            result["policy_version"] = self.policy_version
        return result


@dataclass(frozen=True)
class LifecycleEventRecord:
    """内部 lifecycle event；evidence_refs 只能含脱敏 opaque 引用。"""

    id: str
    owner_platform_user_id: str
    universe_id: str
    resident_id: str
    event_type: str
    status: str
    policy_version: str
    evidence_window_start: str
    evidence_window_end: str
    evidence_count: int
    evidence_refs: Tuple[Mapping[str, object], ...]
    cooldown_until: Optional[str]
    crisis_freeze_until: Optional[str]
    last_resident_exception_requested: bool
    idempotency_key: str
    request_fingerprint: str


@dataclass(frozen=True)
class LifecycleActionRecord:
    """append-only lifecycle 审计动作。"""

    id: str
    event_id: str
    action: str
    actor_type: str
    actor_id: Optional[str]
    metadata: Mapping[str, object]
    created_at: str


def lifecycle_transition_allowed(current_status: str, target_status: str) -> bool:
    """返回状态迁移是否符合冻结的 lifecycle 状态机。"""
    return target_status in _TRANSITIONS.get(current_status, frozenset())


def normalize_lifecycle_category(value: object) -> str:
    """规范 moderation/evaluator category 前缀，保留稳定业务分类。"""
    category = str(value or "").strip().lower()
    for prefix in ("lifecycle:", "cat:"):
        if category.startswith(prefix):
            category = category[len(prefix) :]
    return category


def inactivity_threshold_at(
    *, interaction_anchor_at: datetime, policy: LifecyclePolicy
) -> datetime:
    """返回连续无入站达到 inactivity 门槛的精确时刻。"""
    return interaction_anchor_at + timedelta(days=policy.inactivity_days)


def inactivity_is_due(
    *, now: datetime, interaction_anchor_at: datetime, policy: LifecyclePolicy
) -> bool:
    """精确执行 ``now >= threshold_at`` 的 inactivity 判定。"""
    return now >= inactivity_threshold_at(
        interaction_anchor_at=interaction_anchor_at,
        policy=policy,
    )


def value_misalignment_evidence(
    *,
    now: datetime,
    observations: Sequence[LifecycleEvidenceRef],
    policy: LifecyclePolicy,
) -> Tuple[LifecycleEvidenceRef, ...]:
    """筛出当前滚动窗口内、满足数量与跨度要求的独立价值不相容证据。"""
    window_start = now - timedelta(days=policy.evidence_window_days)
    deduped: dict[tuple[str, str], LifecycleEvidenceRef] = {}
    for item in observations:
        if normalize_lifecycle_category(item.category) != "value_misalignment":
            continue
        if not window_start <= item.observed_at <= now:
            continue
        deduped[(item.source_type, item.source_id)] = item
    ordered = tuple(
        sorted(deduped.values(), key=lambda item: (item.observed_at, item.source_id))
    )
    if len(ordered) < policy.mismatch_min_events:
        return ()
    if ordered[-1].observed_at - ordered[0].observed_at < timedelta(
        days=policy.mismatch_min_span_days
    ):
        return ()
    return ordered


def latest_crisis_freeze_until(
    *,
    now: datetime,
    observations: Sequence[LifecycleEvidenceRef],
    policy: LifecyclePolicy,
) -> Optional[datetime]:
    """返回仍生效的最新 crisis/vulnerability freeze 截止时间。"""
    latest = max(
        (
            item.observed_at
            for item in observations
            if normalize_lifecycle_category(item.category)
            in LIFECYCLE_CRISIS_CATEGORIES
            and item.observed_at <= now
        ),
        default=None,
    )
    if latest is None:
        return None
    freeze_until = latest + timedelta(days=policy.crisis_freeze_days)
    return freeze_until if now < freeze_until else None


def severe_abuse_evidence(
    *, now: datetime, observations: Sequence[LifecycleEvidenceRef], policy: LifecyclePolicy
) -> Tuple[LifecycleEvidenceRef, ...]:
    """返回当前证据窗口内的严重攻击/威胁/仇恨骚扰结构化信号。"""
    window_start = now - timedelta(days=policy.evidence_window_days)
    return tuple(
        sorted(
            (
                item
                for item in observations
                if normalize_lifecycle_category(item.category)
                in LIFECYCLE_SEVERE_ABUSE_CATEGORIES
                and window_start <= item.observed_at <= now
            ),
            key=lambda item: (item.observed_at, item.source_id),
        )
    )


def redact_lifecycle_evidence_refs(
    refs: Sequence[Mapping[str, object]],
) -> Tuple[Mapping[str, object], ...]:
    """把持久化证据再次收敛为 admin 可返回的固定字段白名单。"""
    allowed = {
        "source_type",
        "source_id",
        "category",
        "observed_at",
        "confidence",
        "policy_version",
    }
    return tuple(
        {key: value for key, value in item.items() if key in allowed}
        for item in refs
    )


def lifecycle_event_fingerprint(
    *,
    resident_id: str,
    event_type: str,
    policy_version: str,
    evidence_window_start: str,
    evidence_window_end: str,
    evidence_count: int,
    evidence_refs: Sequence[Mapping[str, object]],
    cooldown_until: Optional[str],
    crisis_freeze_until: Optional[str],
    last_resident_exception_requested: bool,
) -> str:
    """生成稳定 fingerprint，防止相同幂等键静默接受不同证据。"""
    canonical = json.dumps(
        {
            "resident_id": resident_id,
            "event_type": event_type,
            "policy_version": policy_version,
            "evidence_window_start": evidence_window_start,
            "evidence_window_end": evidence_window_end,
            "evidence_count": int(evidence_count),
            "evidence_refs": list(evidence_refs),
            "cooldown_until": cooldown_until,
            "crisis_freeze_until": crisis_freeze_until,
            "last_resident_exception_requested": bool(
                last_resident_exception_requested
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LifecycleRepository(Protocol):
    """M4 lifecycle 持久化端口；实现位于 platform adapter。"""

    def create_event(
        self,
        *,
        event: LifecycleEventRecord,
        actor_type: str,
        actor_id: Optional[str],
        created_at: str,
    ) -> tuple[LifecycleEventRecord, bool]: ...

    def get_event(self, event_id: str) -> Optional[LifecycleEventRecord]: ...

    def list_review_queue(
        self, *, statuses: Sequence[str], limit: int
    ) -> Sequence[LifecycleEventRecord]: ...

    def list_actions(self, event_id: str) -> Sequence[LifecycleActionRecord]: ...

    def transition_event(
        self,
        *,
        event_id: str,
        expected_status: str,
        new_status: str,
        action: str,
        actor_type: str,
        actor_id: Optional[str],
        now: str,
        terminal_reason: Optional[str],
    ) -> Optional[LifecycleEventRecord]: ...


__all__ = [
    "LIFECYCLE_CRISIS_CATEGORIES",
    "LIFECYCLE_EVENT_STATUSES",
    "LIFECYCLE_EVENT_TYPES",
    "LIFECYCLE_SEVERE_ABUSE_CATEGORIES",
    "LifecycleActionRecord",
    "LifecycleEvidenceRef",
    "LifecycleEventRecord",
    "LifecyclePolicy",
    "LifecycleRepository",
    "inactivity_is_due",
    "inactivity_threshold_at",
    "latest_crisis_freeze_until",
    "lifecycle_event_fingerprint",
    "lifecycle_transition_allowed",
    "normalize_lifecycle_category",
    "redact_lifecycle_evidence_refs",
    "severe_abuse_evidence",
    "value_misalignment_evidence",
]
