import hashlib
from typing import Any, Dict, Optional

from app.config import settings
from app.moderation.models import SamplingDecision, normalize_risk_level


POLICY_VERSION = "moderation_policy_v1"


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_from_state(risk_state: Optional[Dict[str, Any]], key: str, default: float) -> float:
    if not risk_state:
        return default
    try:
        return float(risk_state.get(key, default))
    except (TypeError, ValueError):
        return default


def _risk_level_from_state(risk_state: Optional[Dict[str, Any]]) -> str:
    if not risk_state:
        return "normal"
    return str(risk_state.get("risk_level") or "normal").strip().lower() or "normal"


def _deterministic_bucket(
    *,
    account_id: str,
    source_type: str,
    source_id: str,
    policy_version: str,
) -> int:
    raw = f"{policy_version}:{account_id}:{source_type}:{source_id}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest(), 16) % 100


def should_run_llm_review(
    *,
    account_id: str,
    direction: str,
    content_kind: str,
    source_type: str,
    source_id: str,
    text_length: int,
    risk_state: Optional[Dict[str, Any]] = None,
    rule_level: str = "pass",
) -> SamplingDecision:
    """Apply deterministic sampling rules for asynchronous LLM moderation."""

    normalized_rule_level = normalize_risk_level(rule_level)
    if normalized_rule_level in {"block", "escalate"}:
        return SamplingDecision(
            run_llm_review=False,
            sample_rate_percent=100,
            sampling_reason=f"rule_{normalized_rule_level}",
            policy_version=POLICY_VERSION,
        )
    if normalized_rule_level == "review":
        return SamplingDecision(
            run_llm_review=True,
            sample_rate_percent=100,
            sampling_reason="rule_review",
            policy_version=POLICY_VERSION,
        )

    normalized_direction = str(direction or "").strip().lower()
    normalized_source_type = str(source_type or "").strip().lower()
    normalized_content_kind = str(content_kind or "").strip().lower()
    risk_level = _risk_level_from_state(risk_state)

    if normalized_source_type == "outbound_message":
        base_rate = _int_setting("moderation_proactive_sample_percent", 100)
        reason = "proactive_default"
    elif normalized_direction == "outbound":
        base_rate = _int_setting("moderation_outbound_sample_percent", 30)
        reason = "outbound_default"
    else:
        base_rate = _int_setting("moderation_inbound_sample_percent", 15)
        reason = "inbound_default"

    short_text_limit = _int_setting("moderation_short_text_skip_chars", 8)
    if (
        normalized_content_kind in {"text", "voice_transcript"}
        and text_length < short_text_limit
        and risk_level not in {"elevated", "restricted", "disabled"}
        and normalized_source_type != "outbound_message"
    ):
        return SamplingDecision(
            run_llm_review=False,
            sample_rate_percent=0,
            sampling_reason="short_text_skip",
            policy_version=POLICY_VERSION,
        )

    multiplier = max(0.0, _float_from_state(risk_state, "sample_multiplier", 1.0))
    sample_rate = max(0, min(100, int(round(base_rate * multiplier))))
    bucket = _deterministic_bucket(
        account_id=account_id,
        source_type=source_type,
        source_id=source_id,
        policy_version=POLICY_VERSION,
    )
    return SamplingDecision(
        run_llm_review=bucket < sample_rate,
        sample_rate_percent=sample_rate,
        sampling_reason=reason,
        policy_version=POLICY_VERSION,
    )

