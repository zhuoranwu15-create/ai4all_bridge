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
    trigger_type: Optional[str] = None
    candidate_message: Optional[str] = None
    should_send_score: Optional[int] = None
    when_reason: Optional[str] = None
    content_quality: Optional[str] = None
    risk_tag: Optional[str] = None
    l0_context: Optional[Dict[str, Any]] = None
    l1_trigger: Optional[Dict[str, Any]] = None
    l2_when: Optional[Dict[str, Any]] = None
    l3_how: Optional[Dict[str, Any]] = None
    l4_safety: Optional[Dict[str, Any]] = None


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
        + "\n根据更长聊天背景、长期记忆、关系状态、open loops、用户能量状态、主动偏好，按 L0-L4 分层判断。"
        + "\nL1 trigger_type 必须从 explicit_open_loop/emergent_need/critical_synthesis/recovery_followup/ask_to_remember/content_invitation/routine_reminder 中选择。"
        + "\nL2 should_intervene 必须是整数 -2、-1、0、1、2：-2 明确不该发，0 不确定，2 强烈应该发。"
        + "\nL4 风险标签可包含 sensitive_topic/wrong_memory/over_triggering/too_intimate/too_pushy/manipulation_risk。"
        + "\n输出 schema:"
        + "\n{\"l0_context\":{\"recent_session\":\"\",\"long_term_memory\":\"\",\"relationship\":\"\",\"open_loops\":\"\",\"user_energy_state\":\"\",\"user_proactivity_preference\":\"\"},"
        + "\"l1_trigger\":{\"trigger_type\":\"explicit_open_loop\",\"evidence\":\"\"},"
        + "\"l2_when\":{\"should_intervene\":0,\"timing_risk\":\"\",\"false_alarm_risk\":\"\",\"user_autonomy_risk\":\"\"},"
        + "\"l3_how\":{\"target_planning\":\"\",\"dialogue_guidance\":\"\",\"tone\":{\"friend\":\"\",\"system\":\"\",\"marketing\":\"\",\"pushy\":\"\"},\"grounding\":\"\",\"candidate_message\":\"\"},"
        + "\"l4_safety\":{\"sensitive_topic\":false,\"wrong_memory\":false,\"over_triggering\":false,\"too_intimate\":false,\"too_pushy\":false,\"manipulation_risk\":false,\"risk_tags\":[],\"notes\":\"\"},"
        + "\"should_send\":false,\"generated_type\":\""
        + generated_type
        + "\",\"text\":\"\",\"reason\":\"\",\"confidence\":0.0}"
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
            f"user_context: {_clean_text(sample.get('user_context')) or '-'}",
            f"memory_evidence: {_clean_text(sample.get('memory_evidence')) or '-'}",
            f"open_loop: {_clean_text(sample.get('open_loop')) or '-'}",
            f"notes: {_clean_text(sample.get('notes')) or '-'}",
            "long_chat_history:\n" + _history_lines(sample.get("chat_history") or []),
        ]
    )


def _score_from_payload(payload: Dict[str, Any], should_send: bool) -> int:
    try:
        l2_when = payload.get("l2_when") if isinstance(payload.get("l2_when"), dict) else {}
        score = int(payload.get("should_send_score", l2_when.get("should_intervene")))
    except (TypeError, ValueError):
        score = 1 if should_send else -1
    return max(-2, min(score, 2))


def _normalize_generation_payload(
    payload: Dict[str, Any],
    *,
    scenario_type: str,
    raw: str,
) -> ProactiveTestGenerationResult:
    l0_context = payload.get("l0_context") if isinstance(payload.get("l0_context"), dict) else {}
    l1_trigger = payload.get("l1_trigger") if isinstance(payload.get("l1_trigger"), dict) else {}
    l2_when = payload.get("l2_when") if isinstance(payload.get("l2_when"), dict) else {}
    l3_how = payload.get("l3_how") if isinstance(payload.get("l3_how"), dict) else {}
    l4_safety = payload.get("l4_safety") if isinstance(payload.get("l4_safety"), dict) else {}
    candidate_message = _clean_text(
        payload.get("candidate_message")
        or l3_how.get("candidate_message")
        or payload.get("text")
        or payload.get("generated_text")
    )
    should_send = bool(payload.get("should_send"))
    score = _score_from_payload(payload, should_send)
    if "should_send" not in payload:
        should_send = score > 0
    text = candidate_message
    when_reason = _clean_text(payload.get("when_reason") or l1_trigger.get("evidence") or payload.get("reason"))
    reason = _clean_text(payload.get("reason") or when_reason)
    trigger_type = _clean_text(payload.get("trigger_type") or l1_trigger.get("trigger_type")) or scenario_type
    generated_type = _clean_text(payload.get("generated_type")) or trigger_type
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))
    status = "success" if score > 0 and text else "skip"
    return ProactiveTestGenerationResult(
        generated_type=generated_type,
        generated_text=text or None,
        model_should_send=should_send,
        model_confidence=confidence,
        model_reason=reason or None,
        model_raw_json=payload or raw,
        generation_status=status,
        trigger_type=trigger_type,
        candidate_message=candidate_message or None,
        should_send_score=score,
        when_reason=when_reason or None,
        content_quality=_clean_text(payload.get("content_quality") or l3_how.get("dialogue_guidance")) or None,
        risk_tag=_clean_text(payload.get("risk_tag") or ",".join(l4_safety.get("risk_tags") or [])) or None,
        l0_context=l0_context,
        l1_trigger=l1_trigger,
        l2_when=l2_when,
        l3_how=l3_how,
        l4_safety=l4_safety,
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
