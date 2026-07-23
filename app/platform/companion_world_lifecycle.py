"""M4 lifecycle platform service：证据读取、候选评估与可逆审核状态推进。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

from app.config import settings
from app.db import (
    create_resident_lifecycle_event,
    get_resident_lifecycle_event,
    list_resident_lifecycle_event_actions,
    list_resident_lifecycle_events_for_review,
    list_resident_lifecycle_scopes,
    transition_resident_lifecycle_event,
)
from app.domains.companion_world.lifecycle import (
    LifecycleEvidenceRef,
    LifecyclePolicy,
    inactivity_is_due,
    latest_crisis_freeze_until,
    lifecycle_event_fingerprint,
    redact_lifecycle_evidence_refs,
    severe_abuse_evidence,
    value_misalignment_evidence,
)
from app.time_utils import parse_db_timestamp
from app.world_lifecycle.evidence import StructuredLifecycleEvidenceAdapter

LIFECYCLE_POLICY_FAMILY = "companion_world_lifecycle_v1"


def build_lifecycle_policy(config: Any = settings) -> LifecyclePolicy:
    """从配置构造有界策略；非法组合 fail-fast，避免静默漂移安全门。"""
    policy = LifecyclePolicy(
        version="",
        inactivity_days=int(config.companion_world_lifecycle_inactivity_days),
        evidence_window_days=int(
            config.companion_world_lifecycle_evidence_window_days
        ),
        mismatch_min_events=int(
            config.companion_world_lifecycle_mismatch_min_events
        ),
        mismatch_min_span_days=int(
            config.companion_world_lifecycle_mismatch_min_span_days
        ),
        cooldown_days=int(config.companion_world_lifecycle_cooldown_days),
        crisis_freeze_days=int(
            config.companion_world_lifecycle_crisis_freeze_days
        ),
    )
    if not 1 <= policy.inactivity_days <= 365:
        raise ValueError("lifecycle inactivity_days must be between 1 and 365")
    if not 1 <= policy.evidence_window_days <= 180:
        raise ValueError("lifecycle evidence_window_days must be between 1 and 180")
    if not 1 <= policy.mismatch_min_events <= 100:
        raise ValueError("lifecycle mismatch_min_events must be between 1 and 100")
    if not 0 <= policy.mismatch_min_span_days <= policy.evidence_window_days:
        raise ValueError("lifecycle mismatch span must fit evidence window")
    if not 1 <= policy.cooldown_days <= 90:
        raise ValueError("lifecycle cooldown_days must be between 1 and 90")
    if not 1 <= policy.crisis_freeze_days <= 180:
        raise ValueError("lifecycle crisis_freeze_days must be between 1 and 180")
    version = (
        f"{LIFECYCLE_POLICY_FAMILY}:i{policy.inactivity_days}:"
        f"w{policy.evidence_window_days}:n{policy.mismatch_min_events}:"
        f"s{policy.mismatch_min_span_days}:c{policy.cooldown_days}:"
        f"f{policy.crisis_freeze_days}"
    )
    return LifecyclePolicy(**{**policy.__dict__, "version": version})


def _db_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _metrics() -> Dict[str, int]:
    return {
        "scanned": 0,
        "candidate_created": 0,
        "cooldown_advanced": 0,
        "recovery_cancelled": 0,
        "crisis_frozen": 0,
        "last_resident_blocked": 0,
        "legacy_skipped": 0,
        "review_pending": 0,
        "commit_success": 0,
        "commit_failure": 0,
    }


class CompanionWorldLifecycleService:
    """执行一页确定性 shadow evaluation；不包含任何 offline 提交能力。"""

    def __init__(
        self,
        *,
        policy: LifecyclePolicy,
        evidence_adapter: Optional[StructuredLifecycleEvidenceAdapter] = None,
    ) -> None:
        self.policy = policy
        self.evidence_adapter = evidence_adapter or StructuredLifecycleEvidenceAdapter()

    def evaluate_batch(
        self,
        *,
        now: datetime,
        after_resident_id: Optional[str],
        batch_size: int,
    ) -> Dict[str, Any]:
        """按 resident id 分页评估；唯一约束保证并发 scheduler 不重复建 open event。"""
        scopes = list_resident_lifecycle_scopes(
            after_resident_id=after_resident_id,
            limit=batch_size,
        )
        metrics = _metrics()
        results: list[Dict[str, str]] = []
        for scope in scopes:
            metrics["scanned"] += 1
            result = self._evaluate_scope(scope=scope, now=now, metrics=metrics)
            results.append(result)
        return {
            "metrics": metrics,
            "results": results,
            "next_after_resident_id": (
                str(scopes[-1]["resident_id"]) if len(scopes) >= batch_size else None
            ),
        }

    def _evaluate_scope(
        self,
        *,
        scope: Mapping[str, Any],
        now: datetime,
        metrics: Dict[str, int],
    ) -> Dict[str, str]:
        resident_id = str(scope["resident_id"])
        if scope.get("origin") == "legacy":
            metrics["legacy_skipped"] += 1
            return {"resident_id": resident_id, "status": "legacy_skipped"}
        runtime_account_id = str(scope.get("runtime_account_id") or "").strip()
        if not runtime_account_id:
            return {"resident_id": resident_id, "status": "runtime_missing"}

        observations = self.evidence_adapter.observations_for_resident(
            resident_id=resident_id,
            runtime_account_id=runtime_account_id,
            now=now,
            policy=self.policy,
        )
        severe = severe_abuse_evidence(
            now=now,
            observations=observations,
            policy=self.policy,
        )
        crisis_until = latest_crisis_freeze_until(
            now=now,
            observations=observations,
            policy=self.policy,
        )
        mismatch = value_misalignment_evidence(
            now=now,
            observations=observations,
            policy=self.policy,
        )
        inactivity = self._inactivity_evidence(scope=scope, now=now)
        active_count = int(scope.get("active_resident_count") or 0)
        event_id = str(scope.get("open_event_id") or "").strip()
        if event_id:
            return self._revalidate_open_event(
                scope=scope,
                now=now,
                severe=severe,
                mismatch=mismatch,
                inactivity=inactivity,
                crisis_until=crisis_until,
                active_count=active_count,
                metrics=metrics,
            )

        if severe:
            return self._create_candidate(
                scope=scope,
                now=now,
                event_type="severe_abuse",
                evidence=severe,
                status="review_pending",
                crisis_until=crisis_until,
                last_resident_exception_requested=active_count <= 1,
                metrics=metrics,
            )
        if crisis_until is not None:
            metrics["crisis_frozen"] += 1
            return {"resident_id": resident_id, "status": "crisis_frozen"}
        if active_count <= 1:
            metrics["last_resident_blocked"] += 1
            return {"resident_id": resident_id, "status": "last_resident_blocked"}
        if mismatch:
            return self._create_candidate(
                scope=scope,
                now=now,
                event_type="value_misalignment",
                evidence=mismatch,
                status="cooling_down",
                crisis_until=None,
                last_resident_exception_requested=False,
                metrics=metrics,
            )
        if inactivity:
            return self._create_candidate(
                scope=scope,
                now=now,
                event_type="inactivity",
                evidence=inactivity,
                status="cooling_down",
                crisis_until=None,
                last_resident_exception_requested=False,
                metrics=metrics,
            )
        return {"resident_id": resident_id, "status": "not_eligible"}

    def _inactivity_evidence(
        self, *, scope: Mapping[str, Any], now: datetime
    ) -> tuple[LifecycleEvidenceRef, ...]:
        last_inbound = parse_db_timestamp(scope.get("last_inbound_at"))
        joined = parse_db_timestamp(scope.get("joined_at")) or parse_db_timestamp(
            scope.get("resident_created_at")
        )
        anchor = last_inbound or joined
        if anchor is None or not inactivity_is_due(
            now=now,
            interaction_anchor_at=anchor,
            policy=self.policy,
        ):
            return ()
        return (
            LifecycleEvidenceRef(
                source_type=("message" if last_inbound else "resident_joined"),
                source_id=str(
                    scope.get("last_inbound_message_id") or scope["resident_id"]
                ),
                category="inactivity",
                observed_at=anchor,
                policy_version=self.policy.version,
            ),
        )

    def _revalidate_open_event(
        self,
        *,
        scope: Mapping[str, Any],
        now: datetime,
        severe: Sequence[LifecycleEvidenceRef],
        mismatch: Sequence[LifecycleEvidenceRef],
        inactivity: Sequence[LifecycleEvidenceRef],
        crisis_until: Optional[datetime],
        active_count: int,
        metrics: Dict[str, int],
    ) -> Dict[str, str]:
        event_type = str(scope["open_event_type"])
        status = str(scope["open_event_status"])
        current_evidence: Sequence[LifecycleEvidenceRef] = {
            "severe_abuse": severe,
            "value_misalignment": mismatch,
            "inactivity": inactivity,
        }.get(event_type, ())
        terminal_reason: Optional[str] = None
        action = "evidence_recovery_cancelled"
        if event_type != "severe_abuse" and crisis_until is not None:
            terminal_reason = "crisis_freeze_active"
            action = "crisis_freeze_cancelled"
            metrics["crisis_frozen"] += 1
        elif event_type != "severe_abuse" and active_count <= 1:
            terminal_reason = "last_resident_protected"
            action = "last_resident_cancelled"
            metrics["last_resident_blocked"] += 1
        elif not current_evidence:
            terminal_reason = "evidence_no_longer_qualifies"
        if terminal_reason:
            updated = transition_resident_lifecycle_event(
                event_id=str(scope["open_event_id"]),
                expected_status=status,
                new_status="cancelled",
                action=action,
                actor_type="scheduler",
                actor_id="world-lifecycle",
                now=_db_time(now),
                terminal_reason=terminal_reason,
                metadata={
                    "crisis_freeze_until": (
                        _db_time(crisis_until) if crisis_until else None
                    )
                },
            )
            if updated is not None:
                metrics["recovery_cancelled"] += 1
            return {
                "resident_id": str(scope["resident_id"]),
                "status": "cancelled" if updated is not None else "state_changed",
            }
        if status == "review_pending":
            metrics["review_pending"] += 1
            return {
                "resident_id": str(scope["resident_id"]),
                "status": "review_pending",
            }
        cooldown_until = parse_db_timestamp(scope.get("cooldown_until"))
        if cooldown_until is None or now < cooldown_until:
            return {
                "resident_id": str(scope["resident_id"]),
                "status": "cooling_down",
            }
        updated = transition_resident_lifecycle_event(
            event_id=str(scope["open_event_id"]),
            expected_status="cooling_down",
            new_status="review_pending",
            action="cooldown_revalidated",
            actor_type="scheduler",
            actor_id="world-lifecycle",
            now=_db_time(now),
            metadata={"evidence_count": len(current_evidence)},
        )
        if updated is not None:
            metrics["cooldown_advanced"] += 1
            metrics["review_pending"] += 1
        return {
            "resident_id": str(scope["resident_id"]),
            "status": "review_pending" if updated is not None else "state_changed",
        }

    def _create_candidate(
        self,
        *,
        scope: Mapping[str, Any],
        now: datetime,
        event_type: str,
        evidence: Sequence[LifecycleEvidenceRef],
        status: str,
        crisis_until: Optional[datetime],
        last_resident_exception_requested: bool,
        metrics: Dict[str, int],
    ) -> Dict[str, str]:
        refs = tuple(item.as_mapping() for item in evidence)
        window_start = (
            now - timedelta(days=self.policy.evidence_window_days)
            if event_type != "inactivity"
            else evidence[0].observed_at
        )
        cooldown_until = (
            now + timedelta(days=self.policy.cooldown_days)
            if status == "cooling_down"
            else None
        )
        fingerprint = lifecycle_event_fingerprint(
            resident_id=str(scope["resident_id"]),
            event_type=event_type,
            policy_version=self.policy.version,
            evidence_window_start=_db_time(window_start),
            evidence_window_end=_db_time(now),
            evidence_count=len(refs),
            evidence_refs=refs,
            cooldown_until=_db_time(cooldown_until) if cooldown_until else None,
            crisis_freeze_until=_db_time(crisis_until) if crisis_until else None,
            last_resident_exception_requested=last_resident_exception_requested,
        )
        try:
            event, created = create_resident_lifecycle_event(
                owner_platform_user_id=str(scope["owner_platform_user_id"]),
                universe_id=str(scope["universe_id"]),
                resident_id=str(scope["resident_id"]),
                event_type=event_type,
                status=status,
                policy_version=self.policy.version,
                evidence_window_start=_db_time(window_start),
                evidence_window_end=_db_time(now),
                evidence_count=len(refs),
                evidence_refs=refs,
                cooldown_until=_db_time(cooldown_until) if cooldown_until else None,
                crisis_freeze_until=(
                    _db_time(crisis_until) if crisis_until else None
                ),
                last_resident_exception_requested=last_resident_exception_requested,
                idempotency_key=(
                    f"lifecycle:v1:{scope['resident_id']}:{event_type}:"
                    f"{fingerprint[:24]}"
                ),
                request_fingerprint=fingerprint,
                actor_type="scheduler",
                actor_id="world-lifecycle",
                created_at=_db_time(now),
            )
        except ValueError as err:
            if str(err) == "lifecycle event already open":
                return {
                    "resident_id": str(scope["resident_id"]),
                    "status": "state_changed",
                }
            raise
        if created:
            metrics["candidate_created"] += 1
        if event["status"] == "review_pending":
            metrics["review_pending"] += 1
        return {
            "resident_id": str(scope["resident_id"]),
            "event_id": str(event["id"]),
            "status": str(event["status"]),
        }


def serialize_lifecycle_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    """生成 staff/admin DTO，evidence 强制白名单脱敏。"""
    item = dict(event)
    item["evidence_refs"] = list(
        redact_lifecycle_evidence_refs(item.get("evidence_refs") or [])
    )
    item["last_resident_exception_requested"] = bool(
        item.get("last_resident_exception_requested")
    )
    item["redacted"] = True
    item.pop("request_fingerprint", None)
    item.pop("idempotency_key", None)
    return item


def get_lifecycle_review_event(event_id: str) -> Optional[Dict[str, Any]]:
    """读取单个脱敏事件和 append-only action 链。"""
    event = get_resident_lifecycle_event(event_id=event_id)
    if event is None:
        return None
    result = serialize_lifecycle_event(event)
    result["actions"] = list_resident_lifecycle_event_actions(event_id=event_id)
    return result


def list_lifecycle_review_events(
    *, statuses: Sequence[str], limit: int
) -> list[Dict[str, Any]]:
    """读取脱敏 review queue。"""
    return [
        serialize_lifecycle_event(item)
        for item in list_resident_lifecycle_events_for_review(
            statuses=statuses,
            limit=limit,
        )
    ]


__all__ = [
    "CompanionWorldLifecycleService",
    "LIFECYCLE_POLICY_FAMILY",
    "build_lifecycle_policy",
    "get_lifecycle_review_event",
    "list_lifecycle_review_events",
    "serialize_lifecycle_event",
]
