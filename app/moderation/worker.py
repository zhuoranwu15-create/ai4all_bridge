import logging
import time
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import (
    claim_queued_content_moderation_tasks,
    insert_content_moderation_result,
    list_content_moderation_results,
    update_content_moderation_task_machine_status,
    upsert_moderation_account_risk_state,
)
from app.moderation import image_review, llm_review
from app.moderation.models import MachineReviewResult, max_risk_level
from app.moderation.sensitive_words import check_text_rules

logger = logging.getLogger("ai4all.moderation.worker")


class MachineReviewAttemptError(RuntimeError):
    """Raised when a machine reviewer fails and the task should retry."""


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _insert_result(task: Dict[str, Any], result: MachineReviewResult) -> None:
    insert_content_moderation_result(
        task_id=str(task["id"]),
        account_id=str(task["account_id"]),
        reviewer_type=result.reviewer_type,
        engine=result.engine,
        engine_version=result.engine_version,
        result_level=result.level,
        categories=result.categories,
        confidence=result.confidence,
        matched_terms=result.matched_terms,
        reason=result.reason,
        raw_result=result.raw_result,
        latency_ms=result.latency_ms,
        error=result.error,
    )


def _rule_result_recorded(task_id: str) -> bool:
    """Return True if a deterministic rule result is already stored for the task."""

    return any(
        str(result.get("reviewer_type") or "") == "rule"
        for result in list_content_moderation_results(task_id=task_id)
    )


def _result_from_rule(task: Dict[str, Any]) -> MachineReviewResult:
    rule = check_text_rules(
        account_id=str(task["account_id"]),
        text=task.get("snapshot_text") or "",
        direction=str(task.get("direction") or ""),
        content_kind=str(task.get("content_kind") or ""),
    )
    return MachineReviewResult(
        reviewer_type="rule",
        engine="local_sensitive_terms",
        engine_version=rule.policy_version,
        level=rule.level,
        categories=rule.categories,
        confidence=rule.confidence,
        matched_terms=rule.matched_terms,
        reason=rule.reason,
        raw_result={"matched_terms": rule.matched_terms},
    )


def _system_result(*, task: Dict[str, Any], level: str, reason: str, error: Optional[str] = None) -> None:
    _insert_result(
        task,
        MachineReviewResult(
            reviewer_type="system",
            engine="moderation_worker",
            engine_version="moderation_worker_v1",
            level=level,
            reason=reason,
            error=error,
            raw_result={"task_id": task.get("id")},
        ),
    )


def _maybe_update_risk_state(
    *,
    task: Dict[str, Any],
    level: str,
    categories: List[str],
) -> None:
    if level not in {"review", "block", "escalate"}:
        return
    score_delta = {"review": 1, "block": 3, "escalate": 5}.get(level, 1)
    multiplier = {"review": 2.0, "block": 3.0, "escalate": 5.0}.get(level, 2.0)
    upsert_moderation_account_risk_state(
        account_id=str(task["account_id"]),
        risk_level="elevated",
        risk_score_delta=score_delta,
        sample_multiplier=multiplier,
        metadata_patch={
            "last_machine_task_id": task.get("id"),
            "last_machine_level": level,
            "last_machine_categories": categories,
        },
    )


def _selected_for_llm(task: Dict[str, Any]) -> bool:
    metadata = task.get("metadata") or {}
    return bool(metadata.get("llm_review_selected"))


def process_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Process one claimed moderation task and update its machine status."""

    results: List[MachineReviewResult] = []
    # 规则结果通常在 enqueue 时已写入 content_moderation_results；这里重算用于聚合 final_level，
    # 仅当该 task 还没有 rule 结果时才落库，避免重复行，同时保证 worker 始终留有一条规则结果。
    rule_result = _result_from_rule(task)
    results.append(rule_result)
    if not _rule_result_recorded(str(task["id"])):
        _insert_result(task, rule_result)

    image_result = image_review.review_image_task(task)
    if image_result is not None:
        results.append(image_result)
        _insert_result(task, image_result)
        if image_result.error:
            raise MachineReviewAttemptError(image_result.error)

    if _selected_for_llm(task):
        llm_result = llm_review.review_text_with_llm(
            account_id=str(task["account_id"]),
            text=str(task.get("snapshot_text") or ""),
            direction=str(task.get("direction") or ""),
            content_kind=str(task.get("content_kind") or ""),
            source_type=str(task.get("source_type") or ""),
            source_id=str(task.get("source_id") or ""),
        )
        if llm_result is None:
            _system_result(task=task, level="pass", reason="llm review skipped because moderation LLM is disabled")
        else:
            results.append(llm_result)
            _insert_result(task, llm_result)
            if llm_result.error:
                raise MachineReviewAttemptError(llm_result.error)

    final_level = max_risk_level([result.level for result in results])
    categories = list(
        dict.fromkeys(
            category
            for result in results
            for category in (result.categories or [])
            if category
        )
    )
    confidence_values = [result.confidence for result in results if result.confidence is not None]
    confidence = max(confidence_values) if confidence_values else None
    status = "machine_passed" if final_level == "pass" else "needs_review"
    updated = update_content_moderation_task_machine_status(
        task_id=str(task["id"]),
        status=status,
        risk_level=final_level,
        risk_categories=categories,
        confidence=confidence,
        last_error=None,
        completed=True,
        clear_claim=True,
    )
    _maybe_update_risk_state(task=task, level=final_level, categories=categories)
    return {
        "task_id": task["id"],
        "status": status,
        "risk_level": final_level,
        "task": updated,
    }


def _handle_task_error(task: Dict[str, Any], error: Exception) -> Dict[str, Any]:
    max_attempts = max(1, _int_setting("moderation_worker_max_attempts", 3))
    attempts = int(task.get("machine_attempts") or 0)
    error_text = str(error)[:500]
    if attempts >= max_attempts:
        _system_result(
            task=task,
            level="error",
            reason="machine review failed too many times",
            error=error_text,
        )
        updated = update_content_moderation_task_machine_status(
            task_id=str(task["id"]),
            status="needs_review",
            risk_level="review",
            risk_categories=[],
            confidence=None,
            last_error=f"machine_review_failed: {error_text}",
            completed=True,
            clear_claim=True,
        )
        _maybe_update_risk_state(task=task, level="review", categories=[])
        return {
            "task_id": task["id"],
            "status": "needs_review",
            "risk_level": "review",
            "error": error_text,
            "task": updated,
        }

    updated = update_content_moderation_task_machine_status(
        task_id=str(task["id"]),
        status="queued",
        risk_level=str(task.get("risk_level") or "unknown"),
        risk_categories=task.get("risk_categories") or [],
        confidence=task.get("confidence"),
        last_error=error_text,
        completed=False,
        clear_claim=True,
    )
    return {
        "task_id": task["id"],
        "status": "retry",
        "error": error_text,
        "task": updated,
    }


def run_once(*, batch_size: Optional[int] = None) -> Dict[str, Any]:
    """Claim and process one worker batch."""

    if not bool(getattr(settings, "moderation_enabled", True)):
        return {"claimed": 0, "processed": 0, "results": []}
    claimed = claim_queued_content_moderation_tasks(
        batch_size=batch_size or _int_setting("moderation_worker_batch_size", 50),
        claim_timeout_seconds=_int_setting("moderation_worker_claim_timeout_seconds", 300),
    )
    results: List[Dict[str, Any]] = []
    for task in claimed:
        try:
            results.append(process_task(task))
        except Exception as err:
            logger.warning("moderation task failed task_id=%s error=%s", task.get("id"), err)
            results.append(_handle_task_error(task, err))
    counts: Dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "claimed": len(claimed),
        "processed": len(results),
        "counts": counts,
        "results": results,
    }


def run_forever(*, interval_seconds: Optional[float] = None, batch_size: Optional[int] = None) -> None:
    """Run the moderation worker loop until the process receives KeyboardInterrupt."""

    interval = float(
        interval_seconds
        if interval_seconds is not None
        else getattr(settings, "moderation_worker_interval_seconds", 5.0)
    )
    while True:
        run_once(batch_size=batch_size)
        time.sleep(max(0.1, interval))

