"""Nooki P1 权威状态机：原子性、幂等、乐观锁、缩小和账号隔离。"""
from __future__ import annotations

import pytest

import app.db as db
from app.db._core import _migration_0048_nooki_state_contract, connect
from app.products.nooki.domain.goal_breakdown.contracts import (
    NookiDomainError,
    PlanDraft,
    STEP_STATUS_ACTIVE,
    STEP_STATUS_DONE,
    STEP_STATUS_PENDING,
    STEP_STATUS_SKIPPED,
    TASK_STATUS_ABANDONED,
    TASK_STATUS_ACTIVE,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_READY,
)
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def _options() -> tuple[PlanDraft, ...]:
    return (
        PlanDraft("tiny", "打开文档写标题", "只写标题", 2),
        PlanDraft("light", "列出三个要点", "不用写完整句", 6),
        PlanDraft("normal", "写完月报初稿", "先不润色", 20),
    )


def _create(svc, user_id, *, operation_id="op:create", source="msg:create"):
    return svc.create_task_with_options(
        platform_user_id=user_id,
        title="写月报",
        raw_goal="我今天想开始写月报",
        options=_options(),
        source_message_id=source,
        operation_id=operation_id,
    )


def _select(svc, user_id, result, *, operation_id="op:select"):
    return svc.select_task_plan(
        result.task.id,
        result.plans[1].id,
        platform_user_id=user_id,
        operation_id=operation_id,
        expected_version=result.task.version,
        source_message_id="msg:select",
    )


def test_m0048_is_idempotent_and_exposes_final_columns(fresh_db):
    with connect() as conn:
        _migration_0048_nooki_state_contract(conn)
        _migration_0048_nooki_state_contract(conn)
        conn.execute(
            "SELECT completed_at, abandoned_at FROM nooki_tasks WHERE 1 = 0"
        ).fetchall()
        conn.execute(
            """
            SELECT description, suggested_minutes, replaces_step_id
            FROM nooki_steps WHERE 1 = 0
            """
        ).fetchall()
        conn.execute(
            "SELECT operation_id FROM nooki_task_events WHERE 1 = 0"
        ).fetchall()


def test_full_single_action_loop_and_terminal_projection(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037921")
    svc = _service()

    created = _create(svc, user["id"])
    assert created.task.status == TASK_STATUS_DRAFT
    assert [plan.mode for plan in created.plans] == ["tiny", "light", "normal"]

    selected = _select(svc, user["id"], created)
    step = svc._repository.get_step(selected.current_step_id)
    assert selected.status == TASK_STATUS_READY
    assert step.status == STEP_STATUS_PENDING
    assert step.description == created.plans[1].description
    assert step.suggested_minutes == 6

    started = svc.start_step(
        step.id,
        platform_user_id=user["id"],
        operation_id="op:start",
        expected_version=selected.version,
        source_message_id="msg:start",
    )
    assert started.status == STEP_STATUS_ACTIVE
    active_task = svc._repository.get_task(created.task.id)
    assert active_task.status == TASK_STATUS_ACTIVE

    completed = svc.complete_step(
        step.id,
        platform_user_id=user["id"],
        operation_id="op:complete",
        expected_version=active_task.version,
        source_message_id="msg:complete",
    )
    assert completed.status == STEP_STATUS_DONE
    done_task = svc._repository.get_task(created.task.id)
    assert done_task.status == TASK_STATUS_DONE
    assert done_task.completed_at is not None

    projection = svc.get_authoritative_state(user["id"], done_task.id)
    assert projection.focus_task.status == TASK_STATUS_DONE
    assert projection.current_step.status == STEP_STATUS_DONE
    assert svc.get_focus_task_id_for_source_message(
        platform_user_id=user["id"], source_message_id="msg:complete"
    ) == done_task.id


def test_every_write_is_idempotent_by_operation_id(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037922")
    svc = _service()

    first = _create(svc, user["id"])
    repeated = _create(svc, user["id"])
    assert repeated.task.id == first.task.id
    assert [p.id for p in repeated.plans] == [p.id for p in first.plans]

    selected = _select(svc, user["id"], first)
    selected_again = _select(svc, user["id"], first)
    assert selected_again.id == selected.id
    assert selected_again.version == selected.version

    started = svc.start_step(
        selected.current_step_id,
        platform_user_id=user["id"],
        operation_id="op:start",
        expected_version=selected.version,
    )
    started_again = svc.start_step(
        selected.current_step_id,
        platform_user_id=user["id"],
        operation_id="op:start",
        expected_version=1,
    )
    assert started_again.id == started.id
    assert started_again.status == STEP_STATUS_ACTIVE

    task = svc._repository.get_task(first.task.id)
    completed = svc.complete_step(
        started.id,
        platform_user_id=user["id"],
        operation_id="op:complete",
        expected_version=task.version,
    )
    completed_again = svc.complete_step(
        started.id,
        platform_user_id=user["id"],
        operation_id="op:complete",
        expected_version=1,
    )
    assert completed_again.id == completed.id
    with connect() as conn:
        rows = conn.execute(
            "SELECT operation_id FROM nooki_task_events ORDER BY operation_id"
        ).fetchall()
    assert [row["operation_id"] for row in rows] == [
        "op:complete",
        "op:create",
        "op:select",
        "op:start",
    ]


@pytest.mark.parametrize("start_first", [False, True])
def test_shrink_skips_old_step_and_inherits_status(fresh_db, start_first):
    phone = "13800037923" if not start_first else "13800037924"
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    svc = _service()
    created = _create(
        svc, user["id"], operation_id=f"op:create:{phone}", source=f"msg:{phone}"
    )
    selected = _select(
        svc, user["id"], created, operation_id=f"op:select:{phone}"
    )
    old_step = svc._repository.get_step(selected.current_step_id)
    expected_status = STEP_STATUS_PENDING
    task = selected
    if start_first:
        old_step = svc.start_step(
            old_step.id,
            platform_user_id=user["id"],
            operation_id=f"op:start:{phone}",
            expected_version=task.version,
        )
        task = svc._repository.get_task(task.id)
        expected_status = STEP_STATUS_ACTIVE

    smaller = svc.shrink_step(
        old_step.id,
        PlanDraft("tiny", "只打开文档", "看到页面即可", 1),
        platform_user_id=user["id"],
        operation_id=f"op:shrink:{phone}",
        expected_version=task.version,
    )
    assert smaller.status == expected_status
    assert smaller.replaces_step_id == old_step.id
    assert smaller.suggested_minutes == 1
    assert svc._repository.get_step(old_step.id).status == STEP_STATUS_SKIPPED
    assert svc._repository.get_task(task.id).current_step_id == smaller.id

    repeated = svc.shrink_step(
        old_step.id,
        PlanDraft("tiny", "会被忽略", None, 1),
        platform_user_id=user["id"],
        operation_id=f"op:shrink:{phone}",
        expected_version=1,
    )
    assert repeated.id == smaller.id


def test_shrink_requires_known_strictly_smaller_minutes(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037925")
    svc = _service()
    created = _create(svc, user["id"])
    selected = _select(svc, user["id"], created)
    step = svc._repository.get_step(selected.current_step_id)
    with pytest.raises(NookiDomainError, match="replacement_not_smaller"):
        svc.shrink_step(
            step.id,
            PlanDraft("tiny", "并没有更小", None, 6),
            platform_user_id=user["id"],
            operation_id="op:bad-shrink",
            expected_version=selected.version,
        )


def test_abandon_is_idempotent_and_skips_current_step(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037926")
    svc = _service()
    created = _create(svc, user["id"])
    selected = _select(svc, user["id"], created)
    abandoned = svc.abandon_task(
        created.task.id,
        platform_user_id=user["id"],
        operation_id="op:abandon",
        expected_version=selected.version,
    )
    assert abandoned.status == TASK_STATUS_ABANDONED
    assert abandoned.abandoned_at is not None
    assert svc._repository.get_step(selected.current_step_id).status == STEP_STATUS_SKIPPED
    repeated = svc.abandon_task(
        created.task.id,
        platform_user_id=user["id"],
        operation_id="op:abandon",
        expected_version=1,
    )
    assert repeated.version == abandoned.version


def test_open_task_limit_version_guard_and_owner_isolation(fresh_db):
    owner = db.create_or_get_platform_user_by_phone(phone="13800037927")
    intruder = db.create_or_get_platform_user_by_phone(phone="13800037928")
    svc = _service()
    created = _create(svc, owner["id"])

    with pytest.raises(NookiDomainError, match="open_task_exists"):
        _create(svc, owner["id"], operation_id="op:create:second", source="msg:second")
    with pytest.raises(NookiDomainError, match="task_not_found"):
        svc.select_task_plan(
            created.task.id,
            created.plans[0].id,
            platform_user_id=intruder["id"],
            operation_id="op:intruder",
        )
    with pytest.raises(NookiDomainError, match="task_version_conflict"):
        svc.select_task_plan(
            created.task.id,
            created.plans[0].id,
            platform_user_id=owner["id"],
            operation_id="op:stale",
            expected_version=999,
        )


@pytest.mark.parametrize(
    "options",
    [
        _options()[:2],
        (
            PlanDraft("tiny", "a", None, 2),
            PlanDraft("light", "b", None, 2),
            PlanDraft("normal", "c", None, 20),
        ),
        (
            PlanDraft("tiny", "a", None, 4),
            PlanDraft("light", "b", None, 6),
            PlanDraft("normal", "c", None, 20),
        ),
    ],
)
def test_domain_rejects_invalid_plan_contract(fresh_db, options):
    user = db.create_or_get_platform_user_by_phone(phone="13800037929")
    with pytest.raises(NookiDomainError, match="plan_options_invalid"):
        _service().create_task_with_options(
            platform_user_id=user["id"],
            title="写月报",
            raw_goal="开始写月报",
            options=options,
            source_message_id="msg:invalid",
            operation_id="op:invalid",
        )


def test_operation_id_cannot_be_reused_for_another_action(fresh_db):
    user = db.create_or_get_platform_user_by_phone(phone="13800037930")
    svc = _service()
    created = _create(svc, user["id"], operation_id="op:shared")
    with pytest.raises(NookiDomainError, match="operation_id_conflict"):
        svc.select_task_plan(
            created.task.id,
            created.plans[0].id,
            platform_user_id=user["id"],
            operation_id="op:shared",
        )
