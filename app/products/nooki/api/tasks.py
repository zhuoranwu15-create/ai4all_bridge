"""Nooki 按钮态确定性 API：选方案/开始/完成，零 LLM 调用（PRD 第十二节）。

直接调 `GoalBreakdownService`，与聊天里工具调用走同一份状态机；`platform_user_id` 一律来自
session `principal`，不信任前端传参，跨账号归属校验在 domain service 内部完成
（`_ensure_task_owner`），非本人任务/step 统一报 `task_not_found`，翻成 404。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.bootstrap.product_registry import NOOKI_APP_ID
from app.db import SessionPrincipal
from app.products.nooki.domain.goal_breakdown.contracts import NookiDomainError
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository
from app.routers.deps import require_product_session

router = APIRouter(tags=["nooki-tasks"])

_require_nooki_session = require_product_session(NOOKI_APP_ID)

_NOT_FOUND_CODES = {"task_not_found", "step_not_found", "plan_not_found"}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _raise_domain_error(err: NookiDomainError) -> None:
    status_code = 404 if err.code in _NOT_FOUND_CODES else 409
    raise HTTPException(status_code=status_code, detail=err.code)


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def _task_dict(task) -> dict:
    return {
        "task_id": task.id,
        "status": task.status,
        "title": task.title,
        "current_step_id": task.current_step_id,
    }


def _step_dict(step) -> dict:
    return {"step_id": step.id, "status": step.status, "title": step.title}


@router.post("/tasks/{task_id}/plans/{plan_id}/select")
def nooki_select_plan(
    task_id: str,
    plan_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    try:
        task = _service().select_task_plan(
            task_id, plan_id, platform_user_id=principal.platform_user_id
        )
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return {"status": "ok", **_task_dict(task)}


@router.post("/steps/{step_id}/start")
def nooki_start_step(
    step_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    try:
        step = _service().start_step(step_id, platform_user_id=principal.platform_user_id)
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return {"status": "ok", **_step_dict(step)}


@router.post("/steps/{step_id}/complete")
def nooki_complete_step(
    step_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(_require_nooki_session),
) -> dict:
    try:
        step = _service().complete_step(step_id, platform_user_id=principal.platform_user_id)
    except NookiDomainError as err:
        _raise_domain_error(err)
    _no_store(response)
    return {"status": "ok", **_step_dict(step)}


__all__ = ["router"]
