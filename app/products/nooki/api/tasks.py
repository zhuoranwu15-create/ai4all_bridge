"""Nooki 确定性按钮 API：直接调用领域服务，零 LLM。"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import SessionPrincipal
from app.products.nooki.application.ui_projection import project_task_view
from app.products.nooki.domain.goal_breakdown.contracts import NookiDomainError
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository
from app.routers.deps import require_product_session

router = APIRouter(tags=["nooki-tasks"])
_require_nooki_session = require_product_session(NOOKI_APP_ID)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NOT_FOUND_CODES = {"task_not_found", "step_not_found", "plan_not_found"}


class TaskActionRequest(BaseModel):
    """所有按钮写操作共用的幂等键和可选乐观锁版本。"""

    client_request_id: str = Field(min_length=8, max_length=64)
    expected_version: Optional[int] = Field(default=None, ge=1)

    @field_validator("client_request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not _REQUEST_ID_RE.fullmatch(value):
            raise ValueError("invalid client_request_id")
        return value


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _raise_domain_error(err: NookiDomainError) -> None:
    status_code = 404 if err.code in _NOT_FOUND_CODES else 409
    raise HTTPException(status_code=status_code, detail=err.code)


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def _operation_id(principal: SessionPrincipal, request_id: str) -> str:
    return f"button:{principal.platform_user_id}:{request_id}"


def _response(
    service: GoalBreakdownService,
    principal: SessionPrincipal,
    *,
    focus_task_id: str,
    request_id: str,
) -> dict:
    projection = service.get_authoritative_state(
        principal.platform_user_id, focus_task_id
    )
    state, cards = project_task_view(projection)
    return {
        "status": "ok",
        "reply": None,
        "state": state,
        "cards": cards,
        "metadata": {"client_request_id": request_id},
    }


@router.post("/tasks/{task_id}/plans/{plan_id}/select")
def nooki_select_plan(
    task_id: str,
    plan_id: str,
    payload: TaskActionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    service = _service()
    try:
        service.select_task_plan(
            task_id,
            plan_id,
            platform_user_id=principal.platform_user_id,
            operation_id=_operation_id(principal, payload.client_request_id),
            expected_version=payload.expected_version,
        )
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return _response(
        service,
        principal,
        focus_task_id=task_id,
        request_id=payload.client_request_id,
    )


@router.post("/steps/{step_id}/start")
def nooki_start_step(
    step_id: str,
    payload: TaskActionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    service = _service()
    try:
        step = service.start_step(
            step_id,
            platform_user_id=principal.platform_user_id,
            operation_id=_operation_id(principal, payload.client_request_id),
            expected_version=payload.expected_version,
        )
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return _response(
        service,
        principal,
        focus_task_id=step.task_id,
        request_id=payload.client_request_id,
    )


@router.post("/steps/{step_id}/complete")
def nooki_complete_step(
    step_id: str,
    payload: TaskActionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    service = _service()
    try:
        step = service.complete_step(
            step_id,
            platform_user_id=principal.platform_user_id,
            operation_id=_operation_id(principal, payload.client_request_id),
            expected_version=payload.expected_version,
        )
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return _response(
        service,
        principal,
        focus_task_id=step.task_id,
        request_id=payload.client_request_id,
    )


@router.post("/tasks/{task_id}/abandon")
def nooki_abandon_task(
    task_id: str,
    payload: TaskActionRequest,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    service = _service()
    try:
        service.abandon_task(
            task_id,
            platform_user_id=principal.platform_user_id,
            operation_id=_operation_id(principal, payload.client_request_id),
            expected_version=payload.expected_version,
        )
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return _response(
        service,
        principal,
        focus_task_id=task_id,
        request_id=payload.client_request_id,
    )


__all__ = ["router"]
