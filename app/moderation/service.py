import hashlib
import json
import logging
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    create_content_moderation_task,
    get_content_moderation_task_by_idempotency_key,
    get_outbound_message,
    insert_content_moderation_result,
)
from app.moderation.models import RuleDecision, SyncModerationDecision
from app.moderation.policy import should_run_llm_review
from app.moderation.sensitive_words import check_text_rules

logger = logging.getLogger("ai4all.moderation.service")


def _enabled() -> bool:
    return bool(getattr(settings, "moderation_enabled", True))


def _llm_enabled() -> bool:
    return bool(getattr(settings, "moderation_llm_enabled", False))


def _json_hash(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _media_to_dict(media: Optional[Any]) -> Dict[str, Any]:
    if media is None:
        return {}
    if isinstance(media, dict):
        raw = dict(media)
    elif hasattr(media, "model_dump"):
        raw = media.model_dump(exclude_none=True)
    elif hasattr(media, "dict"):
        raw = media.dict(exclude_none=True)
    else:
        raw = {}
    allowed_keys = {"media_id", "url", "path", "format", "duration_ms", "size", "sha256", "hash"}
    return {key: raw[key] for key in allowed_keys if key in raw and raw[key] is not None}


def _status_for_rule_and_sampling(rule: RuleDecision, run_llm_review: bool) -> tuple[str, str]:
    if rule.level in {"review", "block", "escalate", "error"}:
        return "needs_review", rule.level
    if run_llm_review and _llm_enabled():
        return "queued", "unknown"
    return "machine_passed", "pass"


def _insert_rule_result(task_id: str, account_id: str, rule: RuleDecision) -> None:
    insert_content_moderation_result(
        task_id=task_id,
        account_id=account_id,
        reviewer_type="rule",
        engine="local_sensitive_terms",
        engine_version=rule.policy_version,
        result_level=rule.level,
        categories=rule.categories,
        confidence=rule.confidence,
        matched_terms=rule.matched_terms,
        reason=rule.reason,
        raw_result={"matched_terms": rule.matched_terms},
    )


def enqueue_message_for_moderation(
    *,
    message_db_id: int,
    account_id: str,
    session_id: Optional[int],
    direction: str,
    content_kind: str,
    text: Optional[str],
    media: Optional[Any] = None,
    source_message_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create a moderation task for a persisted conversation message."""

    if not _enabled():
        return None
    source_id = str(message_db_id)
    idempotency_key = f"message:{account_id}:{message_db_id}:{direction}"
    existing = get_content_moderation_task_by_idempotency_key(idempotency_key=idempotency_key)
    if existing is not None:
        return existing

    media_json = _media_to_dict(media)
    rule = check_text_rules(
        account_id=account_id,
        text=text,
        direction=direction,
        content_kind=content_kind,
    )
    sampling = should_run_llm_review(
        account_id=account_id,
        direction=direction,
        content_kind=content_kind,
        source_type="message",
        source_id=source_id,
        text_length=len(text or ""),
        rule_level=rule.level,
    )
    status, risk_level = _status_for_rule_and_sampling(rule, sampling.run_llm_review)
    task = create_content_moderation_task(
        account_id=account_id,
        session_id=session_id,
        source_type="message",
        source_id=source_id,
        message_db_id=message_db_id,
        outbound_message_id=None,
        direction=direction,
        content_kind=content_kind,
        status=status,
        risk_level=risk_level,
        risk_categories=rule.categories,
        confidence=rule.confidence,
        content_hash=_json_hash(text or "", media_json),
        snapshot_text=text,
        media=media_json,
        sampling_reason=sampling.sampling_reason,
        sample_rate_percent=sampling.sample_rate_percent,
        policy_version=sampling.policy_version,
        prompt_version=getattr(settings, "moderation_llm_prompt_version", None),
        idempotency_key=idempotency_key,
        metadata={
            **(metadata or {}),
            "source_message_id": source_message_id,
            "llm_review_selected": sampling.run_llm_review,
            "llm_enabled": _llm_enabled(),
        },
    )
    _insert_rule_result(task["id"], account_id, rule)
    return task


def enqueue_outbound_for_moderation(*, outbound_message_id: int) -> Optional[Dict[str, Any]]:
    """Create a moderation task for a proactive outbound ledger row."""

    if not _enabled():
        return None
    outbound = get_outbound_message(outbound_message_id=outbound_message_id)
    if outbound is None:
        return None
    account_id = str(outbound["account_id"])
    source_id = str(outbound["id"])
    idempotency_key = f"outbound:{account_id}:{source_id}"
    existing = get_content_moderation_task_by_idempotency_key(idempotency_key=idempotency_key)
    if existing is not None:
        return existing

    text = str(outbound.get("text") or "")
    rule = check_text_rules(
        account_id=account_id,
        text=text,
        direction="outbound",
        content_kind="text",
    )
    sampling = should_run_llm_review(
        account_id=account_id,
        direction="outbound",
        content_kind="text",
        source_type="outbound_message",
        source_id=source_id,
        text_length=len(text),
        rule_level=rule.level,
    )
    status, risk_level = _status_for_rule_and_sampling(rule, sampling.run_llm_review)
    task = create_content_moderation_task(
        account_id=account_id,
        session_id=None,
        source_type="outbound_message",
        source_id=source_id,
        message_db_id=None,
        outbound_message_id=int(outbound["id"]),
        direction="outbound",
        content_kind="text",
        status=status,
        risk_level=risk_level,
        risk_categories=rule.categories,
        confidence=rule.confidence,
        content_hash=_json_hash(text),
        snapshot_text=text,
        media={},
        sampling_reason=sampling.sampling_reason,
        sample_rate_percent=sampling.sample_rate_percent,
        policy_version=sampling.policy_version,
        prompt_version=getattr(settings, "moderation_llm_prompt_version", None),
        idempotency_key=idempotency_key,
        metadata={
            "outbound_source": outbound.get("source"),
            "product_category": outbound.get("product_category"),
            "llm_review_selected": sampling.run_llm_review,
            "llm_enabled": _llm_enabled(),
        },
    )
    _insert_rule_result(task["id"], account_id, rule)
    return task


def create_sync_block_task(
    *,
    account_id: str,
    session_id: Optional[int],
    source_type: str,
    source_id: str,
    direction: str,
    content_kind: str,
    text: str,
    decision: SyncModerationDecision,
    outbound_message_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a pre-send block decision and its original unsafe snapshot."""

    if not _enabled():
        return None
    idempotency_key = f"{source_type}:{account_id}:{source_id}:pre_send"
    existing = get_content_moderation_task_by_idempotency_key(idempotency_key=idempotency_key)
    if existing is not None:
        return existing

    risk_level = decision.level if decision.level in {"block", "escalate"} else "block"
    task = create_content_moderation_task(
        account_id=account_id,
        session_id=session_id,
        source_type=source_type,
        source_id=str(source_id),
        message_db_id=None,
        outbound_message_id=outbound_message_id,
        direction=direction,
        content_kind=content_kind,
        status="blocked",
        risk_level=risk_level,
        risk_categories=decision.categories,
        confidence=1.0 if decision.matched_terms else None,
        content_hash=_json_hash(text),
        snapshot_text=text,
        media={},
        sampling_reason="sync_guard_block",
        sample_rate_percent=100,
        policy_version=decision.policy_version,
        prompt_version=getattr(settings, "moderation_llm_prompt_version", None),
        idempotency_key=idempotency_key,
        metadata={
            **(metadata or {}),
            "sync_guard": True,
            "llm_review_selected": False,
            "llm_enabled": _llm_enabled(),
        },
    )
    rule = RuleDecision(
        level=risk_level,
        categories=decision.categories,
        matched_terms=decision.matched_terms,
        reason=decision.reason,
        confidence=1.0 if decision.matched_terms else None,
        policy_version=decision.policy_version,
    )
    _insert_rule_result(task["id"], account_id, rule)
    return task

