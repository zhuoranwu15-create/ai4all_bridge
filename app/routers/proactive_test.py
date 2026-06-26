"""Internal proactive message test lab APIs."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    create_proactive_test_candidate,
    get_proactive_test_candidate,
    get_proactive_test_sample_by_sample_id,
    get_proactive_test_long_context,
    hydrate_proactive_test_sample_context,
    import_proactive_test_sample,
    list_proactive_test_candidates,
    list_proactive_test_samples,
    proactive_test_stats,
    upsert_proactive_test_review,
)
from app.proactive.test_lab import REJECT_REASONS, SCENARIO_TYPES, generate_proactive_test_candidate
from app.proactive_test.dataset_prepare import prepare_datasets
from app.routers.deps import get_admin_user, verify_admin_auth

router = APIRouter()

_NON_PRODUCTION_ENVS = {"local", "development", "test"}
_SOURCES = {"real_desensitized", "public_dataset", "synthetic", "manual"}
_FINAL_LABELS = {"CD", "FA", "MN", "NR"}
_PROMOTE_LEVELS = {"reject", "keep", "fewshot", "rule", "auto_send"}
_USER_RESPONSES = {"accepted", "ignored", "negative", "opt-out"}


def require_non_production_internal() -> None:
    if str(getattr(settings, "app_env", "") or "").lower() not in _NON_PRODUCTION_ENVS:
        raise HTTPException(status_code=403, detail="proactive test lab is disabled outside non-production environments")


class ProactiveTestChatMessage(BaseModel):
    role: str
    text: str
    created_at: Optional[str] = None


class ProactiveTestSampleIn(BaseModel):
    sample_id: str
    source: str = "manual"
    scenario_type: str
    chat_history: List[ProactiveTestChatMessage]
    silence_hours: Optional[float] = None
    expected_active_message_type: Optional[str] = None
    notes: Optional[str] = None
    user_context: Optional[str] = None
    memory_evidence: Optional[str] = None
    open_loop: Optional[str] = None
    account_id: Optional[str] = None
    session_id: Optional[int] = None
    context_limit: Optional[int] = Field(default=None, ge=1, le=200)
    context_source: Optional[str] = None


class ProactiveTestImportRequest(BaseModel):
    samples: Optional[List[ProactiveTestSampleIn]] = None
    raw_text: Optional[str] = None


class ProactiveTestSessionSampleRequest(BaseModel):
    account_id: str
    session_id: Optional[int] = None
    scenario_type: str
    sample_id: Optional[str] = None
    context_limit: int = Field(default=120, ge=1, le=200)
    silence_hours: Optional[float] = None
    notes: Optional[str] = None


class ProactiveTestBootstrapRequest(BaseModel):
    limit: int = Field(default=300, ge=1, le=2000)
    upsert: bool = False
    output_path: str = "data/generated/proactive_samples.bootstrap.jsonl"


class ProactiveTestGenerateRequest(BaseModel):
    sample_ids: List[str] = Field(default_factory=list)
    dry_run: bool = True


class ProactiveTestReviewRequest(BaseModel):
    candidate_id: str
    human_should_promote: bool
    reject_reason: Optional[str] = None
    tone_score: Optional[int] = None
    pressure_score: Optional[int] = None
    marketing_score: Optional[int] = None
    privacy_risk: bool = False
    hallucination_risk: bool = False
    review_notes: Optional[str] = None
    revised_message: Optional[str] = None
    final_label: Optional[str] = None
    promote_level: Optional[str] = None
    user_response: Optional[str] = None
    l5_outcome: Optional[dict[str, Any]] = None


def _parse_raw_samples(raw_text: str) -> List[dict[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            if isinstance(parsed.get("samples"), list):
                return parsed["samples"]
            return [parsed]
    except json.JSONDecodeError:
        pass
    rows = []
    for index, line in enumerate(text.splitlines(), start=1):
        cleaned = line.strip()
        if not cleaned:
            continue
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as err:
            raise ValueError(f"line {index}: {err}") from err
        if not isinstance(parsed, dict):
            raise ValueError(f"line {index}: expected JSON object")
        rows.append(parsed)
    return rows


def _validate_sample(sample: ProactiveTestSampleIn) -> None:
    if sample.source not in _SOURCES:
        raise ValueError("source is invalid")
    if sample.scenario_type not in SCENARIO_TYPES:
        raise ValueError("scenario_type is invalid")
    for item in sample.chat_history:
        if item.role not in {"user", "assistant", "system"}:
            raise ValueError("chat_history role is invalid")
        if not item.text.strip():
            raise ValueError("chat_history text is required")


def _score(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    if int(value) < 1 or int(value) > 5:
        raise HTTPException(status_code=400, detail=f"{field_name} must be between 1 and 5")
    return int(value)


@router.post("/internal/proactive-test/samples/import")
def import_samples(
    payload: ProactiveTestImportRequest,
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    raw_items: List[Any] = []
    if payload.samples:
        raw_items.extend([item.model_dump() for item in payload.samples])
    if payload.raw_text:
        try:
            raw_items.extend(_parse_raw_samples(payload.raw_text))
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
    imported = 0
    errors = []
    for index, raw in enumerate(raw_items, start=1):
        try:
            sample = ProactiveTestSampleIn.model_validate(raw)
            _validate_sample(sample)
            import_proactive_test_sample(
                sample_id=sample.sample_id,
                source=sample.source,
                scenario_type=sample.scenario_type,
                chat_history=[item.model_dump() for item in sample.chat_history],
                silence_hours=sample.silence_hours,
                expected_active_message_type=sample.expected_active_message_type,
                notes=sample.notes,
                user_context=sample.user_context,
                memory_evidence=sample.memory_evidence,
                open_loop=sample.open_loop,
                account_id=sample.account_id,
                session_id=sample.session_id,
                context_limit=sample.context_limit,
                context_source=sample.context_source,
            )
            imported += 1
        except Exception as err:  # noqa: BLE001 - import should collect per-row errors
            errors.append({"index": index, "sample_id": raw.get("sample_id") if isinstance(raw, dict) else None, "error": str(err)})
    return {"imported": imported, "failed": len(errors), "errors": errors}


@router.post("/internal/proactive-test/samples/bootstrap")
def bootstrap_samples(
    payload: ProactiveTestBootstrapRequest,
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    return prepare_datasets(
        dataset_names=["synthetic"],
        limit=payload.limit,
        output_path=payload.output_path,
        import_db=True,
        upsert=payload.upsert,
    )


@router.post("/internal/proactive-test/samples/from-session")
def create_sample_from_session(
    payload: ProactiveTestSessionSampleRequest,
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    if payload.scenario_type not in SCENARIO_TYPES:
        raise HTTPException(status_code=400, detail="scenario_type is invalid")
    chat_history, context_source = get_proactive_test_long_context(
        account_id=payload.account_id,
        session_id=payload.session_id,
        limit=payload.context_limit,
    )
    if not chat_history:
        raise HTTPException(status_code=404, detail=f"no context found: {context_source}")
    sample_id = (
        payload.sample_id
        or f"session_{payload.account_id}_{payload.session_id or 'recent'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    user_context = f"从真实会话拉取最近 {len(chat_history)} 条消息，来源 {context_source}。"
    sample = import_proactive_test_sample(
        sample_id=sample_id,
        source="real_desensitized",
        scenario_type=payload.scenario_type,
        chat_history=chat_history,
        silence_hours=payload.silence_hours,
        expected_active_message_type=payload.scenario_type,
        notes=payload.notes or "created from real session for proactive test lab",
        user_context=user_context,
        memory_evidence="真实会话 messages 表上下文；请在审核时结合记忆字段校验。",
        open_loop="由审核员根据长聊天上下文判断未完成事项。",
        account_id=payload.account_id,
        session_id=payload.session_id,
        context_limit=payload.context_limit,
        context_source=context_source,
    )
    return {"ok": True, "sample": sample, "message_count": len(chat_history)}


@router.get("/internal/proactive-test/samples")
def get_samples(
    source: Optional[str] = None,
    scenario_type: Optional[str] = None,
    review_status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    return {
        "items": list_proactive_test_samples(
            source=source,
            scenario_type=scenario_type,
            review_status=review_status,
            limit=limit,
            offset=offset,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.post("/internal/proactive-test/generate")
def generate_candidates(
    payload: ProactiveTestGenerateRequest,
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    if payload.dry_run is not True:
        raise HTTPException(status_code=400, detail="only dry_run=true is supported")
    if not payload.sample_ids:
        raise HTTPException(status_code=400, detail="sample_ids is required")
    run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    counts = {"success": 0, "skip": 0, "error": 0}
    candidates = []
    for sample_id in payload.sample_ids:
        sample = get_proactive_test_sample_by_sample_id(sample_id)
        if sample is None:
            result = create_proactive_test_candidate(
                sample_id=sample_id,
                run_id=run_id,
                scenario_type="unknown",
                generated_type=None,
                generated_text=None,
                model_should_send=None,
                model_confidence=None,
                model_reason=None,
                model_raw_json={},
                generation_status="error",
                generation_error="sample_not_found",
            )
        else:
            sample = hydrate_proactive_test_sample_context(sample)
            generated = generate_proactive_test_candidate(sample)
            result = create_proactive_test_candidate(
                sample_id=sample["sample_id"],
                run_id=run_id,
                scenario_type=sample["scenario_type"],
                generated_type=generated.generated_type,
                generated_text=generated.generated_text,
                model_should_send=generated.model_should_send,
                model_confidence=generated.model_confidence,
                model_reason=generated.model_reason,
                model_raw_json=generated.model_raw_json,
                generation_status=generated.generation_status,
                generation_error=generated.generation_error,
                trigger_type=generated.trigger_type,
                candidate_message=generated.candidate_message,
                should_send_score=generated.should_send_score,
                when_reason=generated.when_reason,
                content_quality=generated.content_quality,
                risk_tag=generated.risk_tag,
                l0_context=generated.l0_context,
                l1_trigger=generated.l1_trigger,
                l2_when=generated.l2_when,
                l3_how=generated.l3_how,
                l4_safety=generated.l4_safety,
            )
        status = result.get("generation_status") or "error"
        counts[status if status in counts else "error"] += 1
        candidates.append(result)
    return {
        "run_id": run_id,
        "total": len(payload.sample_ids),
        **counts,
        "candidates": candidates,
    }


@router.get("/internal/proactive-test/candidates")
def get_candidates(
    run_id: Optional[str] = None,
    scenario_type: Optional[str] = None,
    generation_status: Optional[str] = None,
    review_status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    return {
        "items": list_proactive_test_candidates(
            run_id=run_id,
            scenario_type=scenario_type,
            generation_status=generation_status,
            review_status=review_status,
            limit=limit,
            offset=offset,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.post("/internal/proactive-test/reviews")
def save_review(
    payload: ProactiveTestReviewRequest,
    admin_user: dict = Depends(get_admin_user),
    __: None = Depends(require_non_production_internal),
) -> dict:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(status_code=403, detail="admin or staff role required")
    if get_proactive_test_candidate(payload.candidate_id) is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    if payload.reject_reason and payload.reject_reason not in REJECT_REASONS:
        raise HTTPException(status_code=400, detail="reject_reason is invalid")
    if payload.final_label and payload.final_label not in _FINAL_LABELS:
        raise HTTPException(status_code=400, detail="final_label is invalid")
    if payload.promote_level and payload.promote_level not in _PROMOTE_LEVELS:
        raise HTTPException(status_code=400, detail="promote_level is invalid")
    if payload.user_response and payload.user_response not in _USER_RESPONSES:
        raise HTTPException(status_code=400, detail="user_response is invalid")
    review = upsert_proactive_test_review(
        candidate_id=payload.candidate_id,
        human_should_promote=payload.human_should_promote,
        reject_reason=payload.reject_reason,
        tone_score=_score(payload.tone_score, "tone_score"),
        pressure_score=_score(payload.pressure_score, "pressure_score"),
        marketing_score=_score(payload.marketing_score, "marketing_score"),
        privacy_risk=payload.privacy_risk,
        hallucination_risk=payload.hallucination_risk,
        review_notes=payload.review_notes,
        reviewer=str(admin_user.get("id") or admin_user.get("role") or "admin"),
        revised_message=payload.revised_message,
        final_label=payload.final_label,
        promote_level=payload.promote_level,
        user_response=payload.user_response,
        l5_outcome=payload.l5_outcome,
    )
    return {"ok": True, "review": review}


@router.get("/internal/proactive-test/stats")
def get_stats(
    _: None = Depends(verify_admin_auth),
    __: None = Depends(require_non_production_internal),
) -> dict:
    return proactive_test_stats()
