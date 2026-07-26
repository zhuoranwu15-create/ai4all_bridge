"""Nooki 工具 handler：参数解析 + 转调 `GoalBreakdownService`，领域异常翻译成失败结果。

状态机校验、幂等、乐观锁全部在 domain service（见 `app.products.nooki.domain.goal_breakdown.
service`），这里只做 args 解析和 `NookiDomainError` → `{"status": "failed", "error": code}` 的
翻译，不重复业务判断。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from app.db import get_platform_user_id_for_account
from app.products.nooki.domain.goal_breakdown.contracts import (
    NookiDomainError,
    PlanDraft,
    StepRecord,
    TaskRecord,
)
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def _platform_user_id(ctx: "TurnContext") -> str:
    platform_user_id = get_platform_user_id_for_account(account_id=ctx.account_id)
    if not platform_user_id:
        raise NookiDomainError("platform_user_not_found")
    return platform_user_id


def _task_dict(task: TaskRecord) -> Dict[str, Any]:
    return {
        "task_id": task.id,
        "status": task.status,
        "title": task.title,
        "current_step_id": task.current_step_id,
    }


def _step_dict(step: StepRecord) -> Dict[str, Any]:
    return {"step_id": step.id, "status": step.status, "title": step.title}


def _plan_draft(item: Dict[str, Any]) -> PlanDraft:
    return PlanDraft(
        mode=str(item.get("mode") or ""),
        title=str(item.get("title") or ""),
        description=item.get("description"),
        estimated_minutes=item.get("estimated_minutes"),
    )


def handle_nooki_create_task_draft(args: dict, ctx: "TurnContext") -> dict:
    """建任务草稿；source_message_id 固定取当前消息 id，保证同一条用户消息重试幂等。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        task = _service().create_task_draft(
            platform_user_id=platform_user_id,
            title=str(args.get("title") or args.get("raw_goal") or ""),
            raw_goal=str(args.get("raw_goal") or args.get("title") or ""),
            source_message_id=ctx.message_id,
        )
        return {"status": "ok", **_task_dict(task)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_create_step_options(args: dict, ctx: "TurnContext") -> dict:
    """写入三档方案（tiny/light/normal 缺一不可）。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        options = tuple(_plan_draft(item) for item in (args.get("options") or []))
        task = _service().create_step_options(
            str(args.get("task_id") or ""), options, platform_user_id=platform_user_id
        )
        return {"status": "ok", **_task_dict(task)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_select_task_plan(args: dict, ctx: "TurnContext") -> dict:
    """用户选中一档方案：任务转 active，生成待开始的 step。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        task = _service().select_task_plan(
            str(args.get("task_id") or ""),
            str(args.get("plan_id") or ""),
            platform_user_id=platform_user_id,
        )
        return {"status": "ok", **_task_dict(task)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_start_step(args: dict, ctx: "TurnContext") -> dict:
    """pending → active。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        step = _service().start_step(
            str(args.get("step_id") or ""), platform_user_id=platform_user_id
        )
        return {"status": "ok", **_step_dict(step)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_complete_step(args: dict, ctx: "TurnContext") -> dict:
    """active → done；任务是否整体完成由 nooki_complete_task 另行调用。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        step = _service().complete_step(
            str(args.get("step_id") or ""), platform_user_id=platform_user_id
        )
        return {"status": "ok", **_step_dict(step)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_shrink_step(args: dict, ctx: "TurnContext") -> dict:
    """当前 step 转 skipped，新建一个更小的 active step。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        new_step = args.get("new_step") or {}
        step = _service().shrink_step(
            str(args.get("step_id") or ""),
            _plan_draft(new_step),
            platform_user_id=platform_user_id,
        )
        return {"status": "ok", **_step_dict(step)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_complete_task(args: dict, ctx: "TurnContext") -> dict:
    """任务整体完成（终态）。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        task = _service().complete_task(
            str(args.get("task_id") or ""), platform_user_id=platform_user_id
        )
        return {"status": "ok", **_task_dict(task)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_abandon_task(args: dict, ctx: "TurnContext") -> dict:
    """放弃任务（终态）。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        task = _service().abandon_task(
            str(args.get("task_id") or ""), platform_user_id=platform_user_id
        )
        return {"status": "ok", **_task_dict(task)}
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


def handle_nooki_list_state(args: dict, ctx: "TurnContext") -> dict:
    """读取用户当前聚焦任务/step 的最新状态；无副作用。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        focus_task_id: Optional[str] = args.get("focus_task_id") or None
        projection = _service().get_authoritative_state(platform_user_id, focus_task_id)
        if projection.focus_task is None:
            return {
                "status": "ok",
                "has_focus_task": False,
                "active_tasks_count": projection.active_tasks_count,
            }
        return {
            "status": "ok",
            "has_focus_task": True,
            **_task_dict(projection.focus_task),
            "current_step": _step_dict(projection.current_step) if projection.current_step else None,
            "plans": [
                {"plan_id": p.id, "mode": p.mode, "title": p.title}
                for p in projection.plans
            ],
            "active_tasks_count": projection.active_tasks_count,
        }
    except NookiDomainError as err:
        return {"status": "failed", "error": err.code}


__all__ = [
    "handle_nooki_abandon_task",
    "handle_nooki_complete_step",
    "handle_nooki_complete_task",
    "handle_nooki_create_step_options",
    "handle_nooki_create_task_draft",
    "handle_nooki_list_state",
    "handle_nooki_select_task_plan",
    "handle_nooki_shrink_step",
    "handle_nooki_start_step",
]
