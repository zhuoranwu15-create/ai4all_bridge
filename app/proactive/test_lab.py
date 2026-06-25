"""Dry-run proactive candidate generation for the internal test lab."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.llm import generate_completion
from app.proactive.account_checks import (
    ACCOUNT_CHECK_CANDIDATE_SYSTEM_PROMPT,
    CONTENT_INVITATION_SYSTEM_PROMPT,
    TOPIC_FOLLOWUP_SYSTEM_PROMPT,
)
from app.time_utils import beijing_naive_now


SCENARIO_TYPES = {"account_check", "reactivation_topic", "content_invitation"}
REJECT_REASONS = {
    "no_clear_reason",
    "too_pushy",
    "too_marketing",
    "too_generic",
    "too_private",
    "too_old_topic",
    "duplicate_topic",
    "wrong_timing",
    "high_risk_advice",
    "hallucinated_fact",
    "too_long",
    "not_friend_like",
    "should_skip",
    "other",
}


@dataclass(frozen=True)
class ProactiveTestGenerationResult:
    generated_type: Optional[str]
    generated_text: Optional[str]
    model_should_send: Optional[bool]
    model_confidence: Optional[float]
    model_reason: Optional[str]
    model_raw_json: Any
    generation_status: str
    generation_error: Optional[str] = None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = _clean_text(text)
    if not cleaned:
        raise ValueError("empty model output")
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        raise ValueError("model output does not contain a JSON object")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model output JSON is not an object")
    return parsed


def _history_lines(chat_history: List[Dict[str, Any]]) -> str:
    lines = []
    for item in chat_history:
        role = _clean_text(item.get("role")) or "user"
        text = _clean_text(item.get("text") or item.get("content"))
        created_at = _clean_text(item.get("created_at"))
        if not text:
            continue
        prefix = f"{created_at} " if created_at else ""
        lines.append(f"- {prefix}{role}: {text}")
    return "\n".join(lines) or "- none"


def _scenario_prompt(scenario_type: str) -> str:
    if scenario_type == "account_check":
        base = ACCOUNT_CHECK_CANDIDATE_SYSTEM_PROMPT
        generated_type = "account_check"
    elif scenario_type == "reactivation_topic":
        base = TOPIC_FOLLOWUP_SYSTEM_PROMPT
        generated_type = "reactivation_topic"
    elif scenario_type == "content_invitation":
        base = CONTENT_INVITATION_SYSTEM_PROMPT
        generated_type = "content_invitation"
    else:
        raise ValueError("scenario_type is invalid")
    return (
        base
        + "\n\n你现在运行在主动消息审核台 dry-run 模式。"
        + "\n不要调用工具，不要写数据库，不要发送消息。"
        + "\n只输出 JSON 对象，不输出 Markdown。"
        + "\n输出 schema:"
        + "\n{\"should_send\": false, \"generated_type\": \""
        + generated_type
        + "\", \"text\": \"\", \"reason\": \"\", \"confidence\": 0.0}"
    )


def _build_user_prompt(sample: Dict[str, Any]) -> str:
    now = beijing_naive_now()
    silence_hours = float(sample.get("silence_hours") or 0)
    assumed_now = now + timedelta(hours=max(silence_hours, 0))
    return "\n\n".join(
        [
            f"sample_id: {sample.get('sample_id')}",
            f"scenario_type: {sample.get('scenario_type')}",
            f"source: {sample.get('source')}",
            f"silence_hours: {silence_hours:g}",
            f"assumed_now: {assumed_now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"expected_active_message_type: {_clean_text(sample.get('expected_active_message_type')) or '-'}",
            f"notes: {_clean_text(sample.get('notes')) or '-'}",
            "chat_history:\n" + _history_lines(sample.get("chat_history") or []),
        ]
    )


def _normalize_generation_payload(
    payload: Dict[str, Any],
    *,
    scenario_type: str,
    raw: str,
) -> ProactiveTestGenerationResult:
    should_send = bool(payload.get("should_send"))
    text = _clean_text(payload.get("text") or payload.get("generated_text"))
    reason = _clean_text(payload.get("reason"))
    generated_type = _clean_text(payload.get("generated_type")) or scenario_type
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    status = "success" if should_send and text else "skip"
    return ProactiveTestGenerationResult(
        generated_type=generated_type,
        generated_text=text or None,
        model_should_send=should_send,
        model_confidence=confidence,
        model_reason=reason or None,
        model_raw_json=payload or raw,
        generation_status=status,
    )


def generate_proactive_test_candidate(sample: Dict[str, Any]) -> ProactiveTestGenerationResult:
    """Generate one test-only proactive candidate without touching production state."""
    scenario_type = _clean_text(sample.get("scenario_type"))
    if scenario_type not in SCENARIO_TYPES:
        return ProactiveTestGenerationResult(
            generated_type=None,
            generated_text=None,
            model_should_send=None,
            model_confidence=None,
            model_reason=None,
            model_raw_json={},
            generation_status="error",
            generation_error="scenario_type is invalid",
        )

    messages = [
        {"role": "system", "content": _scenario_prompt(scenario_type)},
        {"role": "user", "content": _build_user_prompt(sample)},
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        return _normalize_generation_payload(payload, scenario_type=scenario_type, raw=raw)
    except Exception as err:  # noqa: BLE001 - test lab records per-sample generation errors
        return ProactiveTestGenerationResult(
            generated_type=scenario_type,
            generated_text=None,
            model_should_send=None,
            model_confidence=None,
            model_reason=None,
            model_raw_json={},
            generation_status="error",
            generation_error=str(err),
        )
