"""`GoalBreakdownService`：Nooki 单任务闭环的领域服务。

抄 app/products/zhaoxi/domain/companion_world/service.py 的模式：构造函数注入仓储，写路径全部包
在 `with self._repository.transaction() as repo:` 里，状态机校验失败抛 `NookiDomainError(code)`，
不碰 SQL/FastAPI。

所有以 task_id/step_id 为入参的写方法都要求调用方传 `platform_user_id` 并在 `_ensure_task_owner`
校验归属（AGENTS.md 的账号隔离不变量在 Nooki 里的等价约束）；不属于该用户的任务/step 统一按
`task_not_found` 处理，不用单独的"无权限"错误码，避免向调用方泄漏 id 是否存在。
"""
from __future__ import annotations

from typing import Optional, Sequence

from app.products.nooki.domain.goal_breakdown.contracts import (
    NookiDomainError,
    PlanDraft,
    STEP_STATUS_ACTIVE,
    STEP_STATUS_DONE,
    STEP_STATUS_PENDING,
    STEP_STATUS_SKIPPED,
    StepRecord,
    TASK_STATUS_ABANDONED,
    TASK_STATUS_ACTIVE,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TASK_STATUS_READY,
    TaskProjection,
    TaskRecord,
    TaskRepository,
)

# 三档粒度固定为 tiny/light/normal（PRD 第六节：最小/轻量/普通三种首个动作粒度）。
PLAN_MODE_COUNT = 3
_VALID_PLAN_MODES = frozenset({"tiny", "light", "normal"})

_TASK_TERMINAL_STATUSES = frozenset({TASK_STATUS_DONE, TASK_STATUS_ABANDONED})

_EVENT_TASK_CREATED = "task_created"
_EVENT_PLANS_CREATED = "plans_created"
_EVENT_PLAN_SELECTED = "plan_selected"
_EVENT_STEP_STARTED = "step_started"
_EVENT_STEP_COMPLETED = "step_completed"
_EVENT_STEP_SHRUNK = "step_shrunk"
_EVENT_TASK_COMPLETED = "task_completed"
_EVENT_TASK_ABANDONED = "task_abandoned"


class GoalBreakdownService:
    """Nooki 目标拆解状态机：任务草稿 → 三档方案 → 单个行动 step 的闭环。"""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    # -- 读路径：直接查仓储，不需要事务 -------------------------------------

    def get_authoritative_state(
        self, platform_user_id: str, focus_task_id: Optional[str] = None
    ) -> TaskProjection:
        """返回前端卡片渲染用的唯一权威投影（PRD 第十三节）。"""

        active_tasks = list(self._repository.list_active_tasks(platform_user_id))
        focus_task: Optional[TaskRecord] = None
        if focus_task_id:
            candidate = self._repository.get_task(focus_task_id)
            if candidate is not None and candidate.platform_user_id == platform_user_id:
                focus_task = candidate
        if focus_task is None and active_tasks:
            focus_task = active_tasks[0]

        if focus_task is None:
            return TaskProjection(focus_task=None, active_tasks_count=len(active_tasks))

        plans: tuple = ()
        if focus_task.status == TASK_STATUS_READY:
            plans = tuple(self._repository.list_plans(focus_task.id))

        current_step: Optional[StepRecord] = None
        if focus_task.current_step_id:
            current_step = self._repository.get_step(focus_task.current_step_id)

        return TaskProjection(
            focus_task=focus_task,
            plans=plans,
            current_step=current_step,
            active_tasks_count=len(active_tasks),
            state_version=focus_task.version,
        )

    # -- 写路径 --------------------------------------------------------------

    def create_task_draft(
        self, *, platform_user_id: str, title: str, raw_goal: str, source_message_id: Optional[str]
    ) -> TaskRecord:
        """建任务草稿；同 `source_message_id` 重试直接返回已存在任务（幂等）。"""

        if source_message_id:
            existing = self._repository.get_task_by_source_message(source_message_id)
            if existing is not None:
                return existing
        with self._repository.transaction() as repo:
            return repo.create_task(
                platform_user_id=platform_user_id,
                title=title,
                raw_goal=raw_goal,
                source_message_id=source_message_id,
            )

    def create_step_options(
        self, task_id: str, options: Sequence[PlanDraft], *, platform_user_id: str
    ) -> TaskRecord:
        """写入三档方案；任务必须还在 draft 且尚未有方案（不支持覆盖重生成）。"""

        self._validate_plan_drafts(options)
        with self._repository.transaction() as repo:
            task = self._require_task(repo, task_id)
            self._ensure_task_owner(task, platform_user_id)
            if task.status != TASK_STATUS_DRAFT:
                raise NookiDomainError("task_already_has_plans")
            repo.create_plans(task_id, options)
            return repo.update_task(
                task_id, expected_version=task.version, status=TASK_STATUS_READY
            )

    def select_task_plan(self, task_id: str, plan_id: str, *, platform_user_id: str) -> TaskRecord:
        """选中一档方案：建一个 pending step（等前端"开始"按钮），任务转 active。"""

        with self._repository.transaction() as repo:
            task = self._require_task(repo, task_id)
            self._ensure_task_owner(task, platform_user_id)
            if task.status != TASK_STATUS_READY:
                raise NookiDomainError("task_not_ready_for_selection")
            plan = repo.get_plan(plan_id)
            if plan is None or plan.task_id != task_id:
                raise NookiDomainError("plan_not_found")

            repo.mark_plan_selected(plan_id)
            step = repo.create_step(
                task_id=task_id,
                plan_id=plan_id,
                title=plan.title,
                status=STEP_STATUS_PENDING,
            )
            updated = repo.update_task(
                task_id,
                expected_version=task.version,
                status=TASK_STATUS_ACTIVE,
                selected_plan_id=plan_id,
                current_step_id=step.id,
            )
            repo.append_task_event(
                task_id=task_id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_PLAN_SELECTED,
                payload=plan_id,
            )
            return updated

    def start_step(self, step_id: str, *, platform_user_id: str) -> StepRecord:
        """pending → active；校验同任务下没有另一个已在跑的 active step。"""

        with self._repository.transaction() as repo:
            step = self._require_step(repo, step_id)
            if step.status != STEP_STATUS_PENDING:
                raise NookiDomainError("step_not_pending")
            task = self._require_task(repo, step.task_id)
            self._ensure_task_owner(task, platform_user_id)
            if task.current_step_id and task.current_step_id != step_id:
                current = repo.get_step(task.current_step_id)
                if current is not None and current.status == STEP_STATUS_ACTIVE:
                    raise NookiDomainError("task_already_has_active_step")

            updated = repo.update_step_status(step_id, STEP_STATUS_ACTIVE)
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_STEP_STARTED,
                payload=step_id,
            )
            return updated

    def complete_step(self, step_id: str, *, platform_user_id: str) -> StepRecord:
        """active → done，写 task_event；任务是否随之整体完成由调用方另行调用 complete_task。"""

        with self._repository.transaction() as repo:
            step = self._require_step(repo, step_id)
            if step.status != STEP_STATUS_ACTIVE:
                raise NookiDomainError("step_not_active")
            task = self._require_task(repo, step.task_id)
            self._ensure_task_owner(task, platform_user_id)

            updated = repo.update_step_status(step_id, STEP_STATUS_DONE)
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_STEP_COMPLETED,
                payload=step_id,
            )
            return updated

    def shrink_step(
        self, step_id: str, new_step: PlanDraft, *, platform_user_id: str
    ) -> StepRecord:
        """当前 step 转 skipped，新建一个更小的 active step（缩小目标法，无需前端"开始"确认）。"""

        with self._repository.transaction() as repo:
            step = self._require_step(repo, step_id)
            if step.status != STEP_STATUS_ACTIVE:
                raise NookiDomainError("step_not_active")
            task = self._require_task(repo, step.task_id)
            self._ensure_task_owner(task, platform_user_id)

            repo.update_step_status(step_id, STEP_STATUS_SKIPPED)
            smaller = repo.create_step(
                task_id=task.id,
                plan_id=step.plan_id,
                title=new_step.title,
                status=STEP_STATUS_ACTIVE,
            )
            repo.update_task(
                task.id, expected_version=task.version, current_step_id=smaller.id
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_STEP_SHRUNK,
                payload=smaller.id,
            )
            return smaller

    def complete_task(self, task_id: str, *, platform_user_id: str) -> TaskRecord:
        """任务整体完成（终态）。"""

        with self._repository.transaction() as repo:
            task = self._require_task(repo, task_id)
            self._ensure_task_owner(task, platform_user_id)
            self._ensure_not_terminal(task)
            updated = repo.update_task(
                task_id, expected_version=task.version, status=TASK_STATUS_DONE
            )
            repo.append_task_event(
                task_id=task_id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_TASK_COMPLETED,
            )
            return updated

    def abandon_task(self, task_id: str, *, platform_user_id: str) -> TaskRecord:
        """放弃任务（终态）。"""

        with self._repository.transaction() as repo:
            task = self._require_task(repo, task_id)
            self._ensure_task_owner(task, platform_user_id)
            self._ensure_not_terminal(task)
            updated = repo.update_task(
                task_id, expected_version=task.version, status=TASK_STATUS_ABANDONED
            )
            repo.append_task_event(
                task_id=task_id,
                platform_user_id=task.platform_user_id,
                event_type=_EVENT_TASK_ABANDONED,
            )
            return updated

    # -- 内部校验辅助 ----------------------------------------------------------

    @staticmethod
    def _validate_plan_drafts(options: Sequence[PlanDraft]) -> None:
        if len(options) != PLAN_MODE_COUNT:
            raise NookiDomainError("plan_options_invalid")
        modes = {draft.mode for draft in options}
        if modes != _VALID_PLAN_MODES:
            raise NookiDomainError("plan_options_invalid")

    @staticmethod
    def _require_task(repo: TaskRepository, task_id: str) -> TaskRecord:
        task = repo.lock_task(task_id)
        if task is None:
            raise NookiDomainError("task_not_found")
        return task

    @staticmethod
    def _require_step(repo: TaskRepository, step_id: str) -> StepRecord:
        step = repo.lock_step(step_id)
        if step is None:
            raise NookiDomainError("step_not_found")
        return step

    @staticmethod
    def _ensure_not_terminal(task: TaskRecord) -> None:
        if task.status in _TASK_TERMINAL_STATUSES:
            raise NookiDomainError("task_already_terminal")

    @staticmethod
    def _ensure_task_owner(task: TaskRecord, platform_user_id: str) -> None:
        """跨账号隔离校验：非本人任务一律当作不存在处理，不额外泄漏"存在但无权限"。"""
        if task.platform_user_id != platform_user_id:
            raise NookiDomainError("task_not_found")


__all__ = ["GoalBreakdownService", "PLAN_MODE_COUNT"]
