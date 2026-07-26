"""GoalBreakdownService 状态机/幂等单测：单任务闭环 + shrink_step 缩小目标法。"""
from __future__ import annotations

import pytest

from app.products.nooki.domain.goal_breakdown.contracts import (
    NookiDomainError,
    PlanDraft,
    TASK_STATUS_ACTIVE,
    TASK_STATUS_DONE,
    TASK_STATUS_READY,
)
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import (
    SqlTaskRepository,
)

import app.db as db


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def test_full_single_task_closed_loop(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037921")
    platform_user_id = user["id"]
    svc = _service()

    task = svc.create_task_draft(
        platform_user_id=platform_user_id,
        title="写完月报",
        raw_goal="这个月的运营月报还没写",
        source_message_id="msg-1",
    )
    assert task.status == "draft"

    # 幂等：同 source_message_id 重试不新建
    task_again = svc.create_task_draft(
        platform_user_id=platform_user_id,
        title="写完月报",
        raw_goal="这个月的运营月报还没写",
        source_message_id="msg-1",
    )
    assert task_again.id == task.id

    task = svc.create_step_options(
        task.id,
        (
            PlanDraft(mode="tiny", title="打开文档写标题"),
            PlanDraft(mode="light", title="列出三个要点"),
            PlanDraft(mode="normal", title="写完初稿"),
        ),
        platform_user_id=platform_user_id,
    )
    assert task.status == TASK_STATUS_READY

    plans = svc._repository.list_plans(task.id)
    assert len(plans) == 3
    tiny_plan = next(p for p in plans if p.mode == "tiny")

    task = svc.select_task_plan(task.id, tiny_plan.id, platform_user_id=platform_user_id)
    assert task.status == TASK_STATUS_ACTIVE
    assert task.current_step_id is not None

    step = svc.start_step(task.current_step_id, platform_user_id=platform_user_id)
    assert step.status == "active"

    step = svc.complete_step(step.id, platform_user_id=platform_user_id)
    assert step.status == "done"

    task = svc.complete_task(task.id, platform_user_id=platform_user_id)
    assert task.status == TASK_STATUS_DONE

    with pytest.raises(NookiDomainError) as excinfo:
        svc.complete_task(task.id, platform_user_id=platform_user_id)
    assert excinfo.value.code == "task_already_terminal"


def test_shrink_step_creates_smaller_active_step(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037922")
    platform_user_id = user["id"]
    svc = _service()
    task = svc.create_task_draft(
        platform_user_id=platform_user_id, title="健身", raw_goal="想开始运动", source_message_id=None
    )
    task = svc.create_step_options(
        task.id,
        (
            PlanDraft(mode="tiny", title="穿上运动鞋"),
            PlanDraft(mode="light", title="做十个俯卧撑"),
            PlanDraft(mode="normal", title="跑步三公里"),
        ),
        platform_user_id=platform_user_id,
    )
    normal_plan = next(p for p in svc._repository.list_plans(task.id) if p.mode == "normal")
    task = svc.select_task_plan(task.id, normal_plan.id, platform_user_id=platform_user_id)
    svc.start_step(task.current_step_id, platform_user_id=platform_user_id)

    smaller = svc.shrink_step(
        task.current_step_id, PlanDraft(mode="tiny", title="只穿鞋"), platform_user_id=platform_user_id
    )
    assert smaller.status == "active"
    assert smaller.id != task.current_step_id

    projection = svc.get_authoritative_state(platform_user_id)
    assert projection.focus_task.id == task.id
    assert projection.current_step.id == smaller.id


def test_write_paths_reject_other_users_task(fresh_db):
    """跨账号隔离：所有 task_id/step_id 写方法必须拒绝非本人任务，统一报 task_not_found。"""

    owner = db.create_or_get_platform_user_by_phone(phone="13800037923")
    intruder = db.create_or_get_platform_user_by_phone(phone="13800037924")
    svc = _service()
    task = svc.create_task_draft(
        platform_user_id=owner["id"], title="写周报", raw_goal="周报还没写", source_message_id=None
    )
    task = svc.create_step_options(
        task.id,
        (
            PlanDraft(mode="tiny", title="打开文档"),
            PlanDraft(mode="light", title="列提纲"),
            PlanDraft(mode="normal", title="写完"),
        ),
        platform_user_id=owner["id"],
    )
    plan = svc._repository.list_plans(task.id)[0]

    with pytest.raises(NookiDomainError) as excinfo:
        svc.select_task_plan(task.id, plan.id, platform_user_id=intruder["id"])
    assert excinfo.value.code == "task_not_found"

    task = svc.select_task_plan(task.id, plan.id, platform_user_id=owner["id"])
    step_id = task.current_step_id

    for call in (
        lambda: svc.start_step(step_id, platform_user_id=intruder["id"]),
        lambda: svc.complete_task(task.id, platform_user_id=intruder["id"]),
        lambda: svc.abandon_task(task.id, platform_user_id=intruder["id"]),
    ):
        with pytest.raises(NookiDomainError) as excinfo:
            call()
        assert excinfo.value.code == "task_not_found"

    step = svc.start_step(step_id, platform_user_id=owner["id"])
    with pytest.raises(NookiDomainError) as excinfo:
        svc.complete_step(step.id, platform_user_id=intruder["id"])
    assert excinfo.value.code == "task_not_found"
    with pytest.raises(NookiDomainError) as excinfo:
        svc.shrink_step(
            step.id, PlanDraft(mode="tiny", title="更小"), platform_user_id=intruder["id"]
        )
    assert excinfo.value.code == "task_not_found"
