"""M4 lifecycle 结构化证据适配器；只输出 opaque 引用，不输出聊天原文。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence, Tuple

from app.db import list_resident_lifecycle_evidence_tasks
from app.domains.companion_world.lifecycle import (
    LIFECYCLE_CRISIS_CATEGORIES,
    LIFECYCLE_SEVERE_ABUSE_CATEGORIES,
    LifecycleEvidenceRef,
    LifecyclePolicy,
    normalize_lifecycle_category,
)
from app.time_utils import parse_db_timestamp

_MODERATION_CONFIRMED_STATUSES = frozenset(
    {"blocked", "escalated", "risk_confirmed"}
)


def _bounded_confidence(value: object) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(confidence, 1.0))


class StructuredLifecycleEvidenceAdapter:
    """从目标 resident runtime 的 moderation 结构化结果生成安全证据引用。"""

    def observations_for_resident(
        self,
        *,
        resident_id: str,
        runtime_account_id: str,
        now: datetime,
        policy: LifecyclePolicy,
    ) -> Tuple[LifecycleEvidenceRef, ...]:
        """读取有界窗口；账号条件由 DB 层强制，返回值不含正文或 account id。"""
        lookback_days = max(
            policy.evidence_window_days,
            policy.crisis_freeze_days,
        )
        tasks = list_resident_lifecycle_evidence_tasks(
            runtime_account_id=runtime_account_id,
            since=(now - timedelta(days=lookback_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            until=now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        observations: list[LifecycleEvidenceRef] = []
        for task in tasks:
            observed_at = parse_db_timestamp(task.get("created_at"))
            if observed_at is None:
                continue
            observations.extend(
                self._explicit_evaluator_observations(
                    resident_id=resident_id,
                    task=task,
                    observed_at=observed_at,
                )
            )
            observations.extend(
                self._moderation_category_observations(
                    task=task,
                    observed_at=observed_at,
                )
            )
        deduped = {
            (item.source_type, item.source_id, item.category): item
            for item in observations
        }
        return tuple(
            sorted(
                deduped.values(),
                key=lambda item: (item.observed_at, item.source_id, item.category),
            )
        )

    @staticmethod
    def _explicit_evaluator_observations(
        *, resident_id: str, task: Mapping[str, Any], observed_at: datetime
    ) -> Sequence[LifecycleEvidenceRef]:
        metadata = task.get("metadata")
        if not isinstance(metadata, Mapping):
            return ()
        raw_items = metadata.get("lifecycle_observations")
        if not isinstance(raw_items, list):
            return ()
        result: list[LifecycleEvidenceRef] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping) or raw.get("confirmed") is not True:
                continue
            scoped_resident_id = str(raw.get("resident_id") or "").strip()
            if scoped_resident_id and scoped_resident_id != resident_id:
                continue
            category = normalize_lifecycle_category(raw.get("category"))
            if category not in {
                "value_misalignment",
                *LIFECYCLE_CRISIS_CATEGORIES,
                *LIFECYCLE_SEVERE_ABUSE_CATEGORIES,
            }:
                continue
            result.append(
                LifecycleEvidenceRef(
                    source_type="moderation_task",
                    source_id=str(task["id"]),
                    category=category,
                    observed_at=observed_at,
                    confidence=_bounded_confidence(
                        raw.get("confidence", task.get("confidence"))
                    ),
                    policy_version=str(
                        raw.get("policy_version")
                        or task.get("policy_version")
                        or ""
                    )
                    or None,
                )
            )
        return result

    @staticmethod
    def _moderation_category_observations(
        *, task: Mapping[str, Any], observed_at: datetime
    ) -> Sequence[LifecycleEvidenceRef]:
        categories = {
            normalize_lifecycle_category(item)
            for item in (task.get("risk_categories") or [])
        }
        risk_level = str(task.get("risk_level") or "").strip().lower()
        status = str(task.get("status") or "").strip().lower()
        result: list[LifecycleEvidenceRef] = []
        for category in sorted(categories & LIFECYCLE_CRISIS_CATEGORIES):
            # 危机信号宁可保守冻结；进入结构化 moderation task 即可生效。
            result.append(
                LifecycleEvidenceRef(
                    source_type="moderation_task",
                    source_id=str(task["id"]),
                    category=category,
                    observed_at=observed_at,
                    confidence=_bounded_confidence(task.get("confidence")),
                    policy_version=str(task.get("policy_version") or "") or None,
                )
            )
        if risk_level in {"block", "escalate"} or status in _MODERATION_CONFIRMED_STATUSES:
            for category in sorted(categories & LIFECYCLE_SEVERE_ABUSE_CATEGORIES):
                result.append(
                    LifecycleEvidenceRef(
                        source_type="moderation_task",
                        source_id=str(task["id"]),
                        category=category,
                        observed_at=observed_at,
                        confidence=_bounded_confidence(task.get("confidence")),
                        policy_version=str(task.get("policy_version") or "") or None,
                    )
                )
        return result


__all__ = ["StructuredLifecycleEvidenceAdapter"]
