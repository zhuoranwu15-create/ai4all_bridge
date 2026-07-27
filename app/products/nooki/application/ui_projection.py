"""把领域 `TaskProjection` 转成 Nooki App 的稳定 state/cards DTO。"""
from __future__ import annotations

from app.products.nooki.domain.goal_breakdown.contracts import (
    STEP_STATUS_ACTIVE,
    STEP_STATUS_PENDING,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_READY,
    TaskProjection,
)


def project_task_state(projection: TaskProjection) -> dict:
    """序列化数据库权威状态；不得使用 LLM 输出补充或覆盖字段。"""

    if projection.focus_task is None:
        return {
            "has_focus_task": False,
            "task": None,
            "current_step": None,
            "plans": [],
            "active_tasks_count": projection.active_tasks_count,
            "state_version": 0,
        }
    task = projection.focus_task
    step = projection.current_step
    return {
        "has_focus_task": True,
        "task": {
            "task_id": task.id,
            "title": task.title,
            "raw_goal": task.raw_goal,
            "status": task.status,
            "selected_plan_id": task.selected_plan_id,
            "current_step_id": task.current_step_id,
            "version": task.version,
            "completed_at": task.completed_at,
            "abandoned_at": task.abandoned_at,
        },
        "current_step": (
            {
                "step_id": step.id,
                "title": step.title,
                "description": step.description,
                "suggested_minutes": step.suggested_minutes,
                "replaces_step_id": step.replaces_step_id,
                "status": step.status,
            }
            if step is not None
            else None
        ),
        "plans": [
            {
                "plan_id": plan.id,
                "mode": plan.mode,
                "title": plan.title,
                "description": plan.description,
                "estimated_minutes": plan.estimated_minutes,
                "is_selected": plan.is_selected,
            }
            for plan in projection.plans
        ],
        "active_tasks_count": projection.active_tasks_count,
        "state_version": projection.state_version,
    }


def project_task_cards(projection: TaskProjection) -> list[dict]:
    """根据领域状态选择 UI 卡片类型；卡片结构不接受模型生成内容。"""

    task = projection.focus_task
    step = projection.current_step
    if task is None:
        return []
    if task.status == TASK_STATUS_DRAFT:
        return [
            {
                "type": "plan_options",
                "task_id": task.id,
                "options": project_task_state(projection)["plans"],
            }
        ]
    if task.status == TASK_STATUS_READY and step is not None and step.status == STEP_STATUS_PENDING:
        return [
            {
                "type": "ready_to_start",
                "task_id": task.id,
                "step": project_task_state(projection)["current_step"],
            }
        ]
    if step is not None and step.status == STEP_STATUS_ACTIVE:
        return [
            {
                "type": "active_step",
                "task_id": task.id,
                "step": project_task_state(projection)["current_step"],
            }
        ]
    if task.status == TASK_STATUS_DONE:
        return [
            {
                "type": "completion",
                "task_id": task.id,
                "title": task.title,
                "message": "你已经完成了这次行动。",
            }
        ]
    return []


def project_task_view(projection: TaskProjection) -> tuple[dict, list[dict]]:
    """一次性返回同一投影对应的 state 与 cards，防止调用方读取两次产生漂移。"""

    return project_task_state(projection), project_task_cards(projection)


__all__ = ["project_task_cards", "project_task_state", "project_task_view"]
