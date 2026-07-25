import hashlib
import json
import logging
from typing import Any, Dict, Mapping, Optional

from app.config import settings
from app.platform.moderation.persistence import (
    create_content_moderation_task,
    get_content_moderation_task_by_idempotency_key,
    insert_content_moderation_result,
)
from app.platform.moderation import aliyun_review
from app.platform.moderation.models import (
    InboundScreenDecision,
    RuleDecision,
    SyncModerationDecision,
    max_risk_level,
)
from app.platform.moderation.policy import should_run_llm_review
from app.platform.moderation.sensitive_words import check_text_rules

logger = logging.getLogger("ai4all.moderation.service")

INBOUND_SYNC_POLICY_VERSION = "moderation_inbound_sync_v1"

# 仅文本类内容走阿里云同步文本云审核；图片等仍走第一阶段异步审核，维持既有边界。
_INBOUND_SYNC_CONTENT_KINDS = {"text", "voice_transcript"}


def _enabled() -> bool:
    return bool(getattr(settings, "moderation_enabled", True))


def _llm_enabled() -> bool:
    return bool(getattr(settings, "moderation_llm_enabled", False))


def _aliyun_inbound_sync_enabled() -> bool:
    """入站是否走阿里云同步筛查：总开关与入站同步开关同时为真。"""

    return bool(getattr(settings, "moderation_aliyun_enabled", False)) and bool(
        getattr(settings, "moderation_aliyun_inbound_sync_enabled", True)
    )


# 视为“放行/已结案”的任务状态，用于幂等命中时的决策判定。
_INBOUND_BLOCKING_STATUSES = {
    "needs_review",
    "reviewing",
    "blocked",
    "escalated",
    "risk_confirmed",
}


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


def screen_inbound_message_sync(
    *,
    message_db_id: int,
    account_id: str,
    session_id: Optional[int],
    content_kind: str,
    text: Optional[str],
    media: Optional[Any] = None,
    source_message_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> InboundScreenDecision:
    """入站用户内容同步筛查：本地红线 + 阿里云云审核，命中即停回复。

    阿里云未开启时回退第一阶段异步行为（enqueue + 放行）；阿里云调用失败时降级为本地规则判定。
    返回的决策用于在 turn_service 决定是否生成本轮 AI 回复。
    """

    if not _enabled():
        return InboundScreenDecision(allowed=True, reason="moderation_disabled")

    # 非文本类（图片等）暂不接入审核：尚未接入图片安全模型，直接放行且不建任务，
    # 避免堆积无法处理的队列任务。接入图片模型后再恢复。文本/语音转写继续走下方审核。
    if content_kind not in _INBOUND_SYNC_CONTENT_KINDS:
        return InboundScreenDecision(allowed=True, reason="inbound_non_text_skipped")

    # 未启用阿里云入站同步：保持第一阶段异步审核，主链路放行。
    if not _aliyun_inbound_sync_enabled():
        enqueue_message_for_moderation(
            message_db_id=message_db_id,
            account_id=account_id,
            session_id=session_id,
            direction="inbound",
            content_kind=content_kind,
            text=text,
            media=media,
            source_message_id=source_message_id,
            metadata=metadata,
        )
        return InboundScreenDecision(allowed=True, reason="aliyun_inbound_sync_disabled")

    idempotency_key = f"message:{account_id}:{message_db_id}:inbound"
    existing = get_content_moderation_task_by_idempotency_key(idempotency_key=idempotency_key)
    if existing is not None:
        status = str(existing.get("status") or "")
        return InboundScreenDecision(
            allowed=status not in _INBOUND_BLOCKING_STATUSES,
            level=str(existing.get("risk_level") or "unknown"),
            categories=list(existing.get("risk_categories") or []),
            task_id=str(existing.get("id")),
            reason="idempotent_existing_task",
        )

    normalized_text = text or ""
    media_json = _media_to_dict(media)

    # 1) 本地补充红线（始终执行，并作为阿里云失败时的降级判定）。
    rule = check_text_rules(
        account_id=account_id,
        text=normalized_text,
        direction="inbound",
        content_kind=content_kind,
    )

    # 2) 阿里云云审核（主引擎）。
    cloud = aliyun_review.review_text_with_aliyun(
        account_id=account_id,
        text=normalized_text,
        data_id=idempotency_key,
    )
    degraded = cloud.level == "error"
    # 降级：阿里云失败不计入风险聚合，以本地规则为准。
    cloud_level_for_agg = "pass" if degraded else cloud.level

    final_level = max_risk_level([rule.level, cloud_level_for_agg])
    categories = list(
        dict.fromkeys(
            [*(rule.categories or []), *([] if degraded else (cloud.categories or []))]
        )
    )
    allowed = final_level == "pass"
    status = "machine_passed" if allowed else "needs_review"

    confidence_values = [c for c in (rule.confidence, cloud.confidence) if c is not None]
    task = create_content_moderation_task(
        account_id=account_id,
        session_id=session_id,
        source_type="message",
        source_id=str(message_db_id),
        message_db_id=message_db_id,
        outbound_message_id=None,
        direction="inbound",
        content_kind=content_kind,
        status=status,
        risk_level=final_level,
        risk_categories=categories,
        confidence=max(confidence_values) if confidence_values else None,
        content_hash=_json_hash(normalized_text, media_json),
        snapshot_text=normalized_text,
        media=media_json,
        sampling_reason="aliyun_inbound_sync_degraded" if degraded else "aliyun_inbound_sync",
        sample_rate_percent=100,
        policy_version=INBOUND_SYNC_POLICY_VERSION,
        prompt_version=None,
        idempotency_key=idempotency_key,
        metadata={
            **(metadata or {}),
            "source_message_id": source_message_id,
            "engine": "aliyun_text_moderation_plus",
            "aliyun_degraded": degraded,
            "llm_review_selected": False,
        },
    )
    # 落两条机器结果：本地规则 + 阿里云（含失败记录）。
    _insert_rule_result(task["id"], account_id, rule)
    insert_content_moderation_result(
        task_id=task["id"],
        account_id=account_id,
        reviewer_type=cloud.reviewer_type,
        engine=cloud.engine,
        engine_version=cloud.engine_version,
        result_level=cloud.level,
        categories=cloud.categories,
        confidence=cloud.confidence,
        matched_terms=cloud.matched_terms,
        reason=cloud.reason,
        raw_result=cloud.raw_result,
        latency_ms=cloud.latency_ms,
        error=cloud.error,
    )
    return InboundScreenDecision(
        allowed=allowed,
        level=final_level,
        categories=categories,
        task_id=str(task["id"]),
        reason=cloud.reason if not degraded else "aliyun_degraded_to_local_rule",
        degraded=degraded,
    )


def enqueue_outbound_for_moderation(
    *, outbound_message: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    """Create a moderation task from a product-owned outbound ledger row."""

    if not _enabled():
        return None
    if outbound_message.get("id") is None or not outbound_message.get("account_id"):
        return None
    outbound = dict(outbound_message)
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
