import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.config import settings
from app.llm import generate_completion, get_active_llm_model
from app.moderation.models import MachineReviewResult, normalize_risk_level

logger = logging.getLogger("ai4all.moderation.llm_review")

_SYSTEM_PROMPT = """You are an independent content safety reviewer for AI4ALL.
Return only JSON with:
level: pass|review|block|escalate
categories: string[]
confidence: number between 0 and 1, or null
reason: short reason
suggested_action: short action
Use review for uncertain or borderline content."""


def _as_categories(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _extract_json_object(text: str) -> Dict[str, Any]:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("llm moderation result must be a JSON object")
    return parsed


def review_text_with_llm(
    *,
    account_id: str,
    text: str,
    direction: str,
    content_kind: str,
    source_type: str,
    source_id: str,
) -> Optional[MachineReviewResult]:
    """Call the independent moderation LLM and return a normalized result."""

    if not bool(getattr(settings, "moderation_llm_enabled", False)):
        return None
    model = get_active_llm_model()

    user_payload = {
        "account_id": account_id,
        "direction": direction,
        "content_kind": content_kind,
        "source_type": source_type,
        "source_id": source_id,
        "text": text,
    }
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    started = time.monotonic()
    try:
        content = generate_completion(messages)
    except Exception as err:  # noqa: BLE001 - moderation worker records machine-review errors
        logger.warning("moderation llm request/parse failed error=%s", err)
        return MachineReviewResult(
            reviewer_type="llm",
            engine="default_llm_provider",
            engine_version=model,
            level="error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error="moderation_llm_request_failed",
            raw_result={"error": str(err)[:1000]},
        )
    if not str(content or "").strip():
        return MachineReviewResult(
            reviewer_type="llm",
            engine="default_llm_provider",
            engine_version=model,
            level="error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error="moderation_llm_empty_response",
        )

    try:
        parsed = _extract_json_object(str(content or ""))
    except (json.JSONDecodeError, ValueError) as err:
        return MachineReviewResult(
            reviewer_type="llm",
            engine="default_llm_provider",
            engine_version=model,
            level="error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error="moderation_llm_schema_invalid",
            raw_result={"error": str(err)[:1000], "content": str(content or "")[:2000]},
        )

    raw_level = str(parsed.get("level") or "").strip().lower()
    if raw_level not in {"pass", "review", "block", "escalate"}:
        return MachineReviewResult(
            reviewer_type="llm",
            engine="default_llm_provider",
            engine_version=model,
            level="error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error="moderation_llm_level_invalid",
            raw_result={"parsed": parsed},
        )

    return MachineReviewResult(
        reviewer_type="llm",
        engine="default_llm_provider",
        engine_version=model,
        level=normalize_risk_level(raw_level),
        categories=_as_categories(parsed.get("categories")),
        confidence=_as_confidence(parsed.get("confidence")),
        reason=str(parsed.get("reason") or "")[:500],
        raw_result={"parsed": parsed},
        latency_ms=int((time.monotonic() - started) * 1000),
    )
