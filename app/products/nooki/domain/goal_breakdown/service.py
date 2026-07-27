"""Nooki P1 单行动闭环的领域服务。

Task/Step 状态、账号归属、乐观锁与幂等全部由本服务裁决。API Router 和 Tool
Handler 只表达业务意图，不得自行推进状态或直接写 SQL。
"""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

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
    TaskEventRecord,
    TaskProjection,
    TaskRecord,
    TaskRepository,
    TaskWithOptionsResult,
)
from app.time_utils import beijing_now_str

PLAN_MODE_COUNT = 3
_VALID_PLAN_MODES = frozenset({"tiny", "light", "normal"})
_PLAN_MINUTE_RANGES = {
    "tiny": (1, 3),
    "light": (3, 10),
    "normal": (8, 30),
}
_TASK_TERMINAL_STATUSES = frozenset({TASK_STATUS_DONE, TASK_STATUS_ABANDONED})

_EVENT_TASK_CREATED = "task_created_with_options"
_EVENT_PLAN_SELECTED = "plan_selected"
_EVENT_STEP_STARTED = "step_started"
_EVENT_STEP_COMPLETED = "step_completed"
_EVENT_STEP_SHRUNK = "step_shrunk"
_EVENT_TASK_ABANDONED = "task_abandoned"
_EVENT_LATER_ITEM_CONVERTED = "later_item_converted"


class GoalBreakdownService:
    """管理 `draft → ready → active → done/abandoned` 的唯一写入口。"""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    def get_authoritative_state(
        self, platform_user_id: str, focus_task_id: Optional[str] = None
    ) -> TaskProjection:
        """读取 UI/LLM 共用的权威投影；显式 focus 可读取刚进入终态的任务。"""

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

        current_step = None
        if focus_task.current_step_id:
            current_step = self._repository.get_step(focus_task.current_step_id)
        return TaskProjection(
            focus_task=focus_task,
            plans=tuple(self._repository.list_plans(focus_task.id)),
            current_step=current_step,
            active_tasks_count=len(active_tasks),
            state_version=focus_task.version,
        )

    def get_focus_task_id_for_source_message(
        self, *, platform_user_id: str, source_message_id: str
    ) -> Optional[str]:
        """返回本条聊天消息最后影响的任务，供重复请求重建终态卡片。"""

        return self._repository.get_latest_task_id_by_source_message(
            platform_user_id=platform_user_id,
            source_message_id=source_message_id,
        )

    def create_task_with_options(
        self,
        *,
        platform_user_id: str,
        title: str,
        raw_goal: str,
        options: Sequence[PlanDraft],
        source_message_id: str,
        operation_id: str,
    ) -> TaskWithOptionsResult:
        """单事务创建 task=draft、三档方案和唯一幂等事件。"""

        self._validate_operation_id(operation_id)
        clean_title, clean_goal = self._validate_task_input(title, raw_goal)
        plan_drafts = self._validate_plan_drafts(options)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_TASK_CREATED, platform_user_id
            )
            if event is not None:
                task = self._require_owned_task(repo, event.task_id, platform_user_id)
                return TaskWithOptionsResult(
                    task=task, plans=tuple(repo.list_plans(task.id))
                )
            if repo.list_active_tasks(platform_user_id):
                # PG READ COMMITTED 下，并发同 operation 可能在首次读取后提交；
                # 看到未终结任务时再查一次 event，避免把并发重试误报为新任务冲突。
                event = self._replay_event(
                    repo, operation_id, _EVENT_TASK_CREATED, platform_user_id
                )
                if event is not None:
                    task = self._require_owned_task(repo, event.task_id, platform_user_id)
                    return TaskWithOptionsResult(
                        task=task, plans=tuple(repo.list_plans(task.id))
                    )
                raise NookiDomainError("open_task_exists")

            task = repo.create_task(
                platform_user_id=platform_user_id,
                title=clean_title,
                raw_goal=clean_goal,
                source_message_id=source_message_id,
            )
            plans = tuple(repo.create_plans(task.id, plan_drafts))
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_TASK_CREATED,
                payload=self._payload(task_id=task.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return TaskWithOptionsResult(task=task, plans=plans)

    def convert_later_item_with_options(
        self,
        item_id: str,
        *,
        platform_user_id: str,
        options: Sequence[PlanDraft],
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> TaskWithOptionsResult:
        """单事务把 inbox 稍后项转换为 Task + 三档方案，并记录幂等事件。"""

        self._validate_operation_id(operation_id)
        plan_drafts = self._validate_plan_drafts(options)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_LATER_ITEM_CONVERTED, platform_user_id
            )
            if event is not None:
                task = self._require_owned_task(repo, event.task_id, platform_user_id)
                return TaskWithOptionsResult(task=task, plans=tuple(repo.list_plans(task.id)))

            item = repo.lock_later_item(item_id)
            if item is None or item.platform_user_id != platform_user_id:
                raise NookiDomainError("later_item_not_found")
            if item.status != "inbox":
                raise NookiDomainError("later_item_not_convertible")
            if expected_version is not None and expected_version != item.version:
                raise NookiDomainError("later_item_version_conflict")
            if repo.list_active_tasks(platform_user_id):
                raise NookiDomainError("open_task_exists")

            title, raw_goal = self._validate_task_input(item.content, item.content)
            task = repo.create_task(
                platform_user_id=platform_user_id,
                title=title,
                raw_goal=raw_goal,
                source_message_id=f"later:{item.id}",
            )
            plans = tuple(repo.create_plans(task.id, plan_drafts))
            repo.mark_later_item_converted(
                item.id, expected_version=item.version, task_id=task.id
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_LATER_ITEM_CONVERTED,
                payload=self._payload(task_id=task.id, later_item_id=item.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return TaskWithOptionsResult(task=task, plans=plans)

    def select_task_plan(
        self,
        task_id: str,
        plan_id: str,
        *,
        platform_user_id: str,
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> TaskRecord:
        """选择方案：Task `draft→ready`，并创建一个 `pending` Step。"""

        self._validate_operation_id(operation_id)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_PLAN_SELECTED, platform_user_id
            )
            if event is not None:
                return self._require_owned_task(repo, event.task_id, platform_user_id)

            task = self._require_owned_task(repo, task_id, platform_user_id)
            event = self._replay_event(
                repo, operation_id, _EVENT_PLAN_SELECTED, platform_user_id
            )
            if event is not None:
                return self._require_owned_task(repo, event.task_id, platform_user_id)
            self._check_expected_version(task, expected_version)
            if task.status != TASK_STATUS_DRAFT:
                raise NookiDomainError("task_not_ready_for_selection")
            if task.current_step_id is not None:
                raise NookiDomainError("task_plan_already_selected")
            plan = repo.get_plan(plan_id)
            if plan is None or plan.task_id != task.id:
                raise NookiDomainError("plan_not_found")

            repo.mark_plan_selected(plan.id)
            step = repo.create_step(
                task_id=task.id,
                plan_id=plan.id,
                title=plan.title,
                description=plan.description,
                suggested_minutes=plan.estimated_minutes,
                replaces_step_id=None,
                status=STEP_STATUS_PENDING,
            )
            updated = repo.update_task(
                task.id,
                expected_version=task.version,
                status=TASK_STATUS_READY,
                selected_plan_id=plan.id,
                current_step_id=step.id,
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_PLAN_SELECTED,
                payload=self._payload(
                    task_id=task.id, plan_id=plan.id, step_id=step.id
                ),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return updated

    def start_step(
        self,
        step_id: str,
        *,
        platform_user_id: str,
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> StepRecord:
        """开始行动：Step `pending→active`，Task `ready→active`。"""

        self._validate_operation_id(operation_id)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_STARTED, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)

            step = self._require_step(repo, step_id)
            task = self._require_owned_task(repo, step.task_id, platform_user_id)
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_STARTED, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)
            self._check_expected_version(task, expected_version)
            if task.current_step_id != step.id:
                raise NookiDomainError("step_not_current")
            if task.status != TASK_STATUS_READY:
                raise NookiDomainError("task_not_ready_for_start")
            if step.status != STEP_STATUS_PENDING:
                raise NookiDomainError("step_not_pending")

            updated = repo.update_step_status(step.id, STEP_STATUS_ACTIVE)
            repo.update_task(
                task.id, expected_version=task.version, status=TASK_STATUS_ACTIVE
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_STEP_STARTED,
                payload=self._payload(task_id=task.id, step_id=step.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return updated

    def complete_step(
        self,
        step_id: str,
        *,
        platform_user_id: str,
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> StepRecord:
        """完成 P1 单行动：Step `active→done`，Task 同时进入 `done`。"""

        self._validate_operation_id(operation_id)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_COMPLETED, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)

            step = self._require_step(repo, step_id)
            task = self._require_owned_task(repo, step.task_id, platform_user_id)
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_COMPLETED, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)
            self._check_expected_version(task, expected_version)
            if task.current_step_id != step.id:
                raise NookiDomainError("step_not_current")
            if task.status != TASK_STATUS_ACTIVE or step.status != STEP_STATUS_ACTIVE:
                raise NookiDomainError("step_not_active")

            updated = repo.update_step_status(step.id, STEP_STATUS_DONE)
            repo.update_task(
                task.id,
                expected_version=task.version,
                status=TASK_STATUS_DONE,
                completed_at=beijing_now_str(),
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_STEP_COMPLETED,
                payload=self._payload(task_id=task.id, step_id=step.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return updated

    def shrink_step(
        self,
        step_id: str,
        replacement: PlanDraft,
        *,
        platform_user_id: str,
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> StepRecord:
        """旧 Step→skipped；新 Step 继承 pending/active，且必须可证明更小。"""

        self._validate_operation_id(operation_id)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_SHRUNK, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)

            step = self._require_step(repo, step_id)
            task = self._require_owned_task(repo, step.task_id, platform_user_id)
            event = self._replay_event(
                repo, operation_id, _EVENT_STEP_SHRUNK, platform_user_id
            )
            if event is not None:
                return self._step_from_event(repo, event, platform_user_id)
            self._check_expected_version(task, expected_version)
            if task.current_step_id != step.id:
                raise NookiDomainError("step_not_current")
            if step.status not in (STEP_STATUS_PENDING, STEP_STATUS_ACTIVE):
                raise NookiDomainError("step_not_shrinkable")
            clean = self._validate_replacement(step, replacement)

            repo.update_step_status(step.id, STEP_STATUS_SKIPPED)
            smaller = repo.create_step(
                task_id=task.id,
                plan_id=step.plan_id,
                title=clean.title,
                description=clean.description,
                suggested_minutes=clean.estimated_minutes,
                replaces_step_id=step.id,
                status=step.status,
            )
            repo.update_task(
                task.id, expected_version=task.version, current_step_id=smaller.id
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_STEP_SHRUNK,
                payload=self._payload(task_id=task.id, step_id=smaller.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return smaller

    def abandon_task(
        self,
        task_id: str,
        *,
        platform_user_id: str,
        operation_id: str,
        expected_version: Optional[int] = None,
        source_message_id: Optional[str] = None,
    ) -> TaskRecord:
        """放弃任意未终结 Task；当前 pending/active Step 同时变 skipped。"""

        self._validate_operation_id(operation_id)
        with self._repository.transaction() as repo:
            event = self._replay_event(
                repo, operation_id, _EVENT_TASK_ABANDONED, platform_user_id
            )
            if event is not None:
                return self._require_owned_task(repo, event.task_id, platform_user_id)

            task = self._require_owned_task(repo, task_id, platform_user_id)
            event = self._replay_event(
                repo, operation_id, _EVENT_TASK_ABANDONED, platform_user_id
            )
            if event is not None:
                return self._require_owned_task(repo, event.task_id, platform_user_id)
            self._check_expected_version(task, expected_version)
            if task.status in _TASK_TERMINAL_STATUSES:
                raise NookiDomainError("task_already_terminal")
            if task.current_step_id:
                current = repo.get_step(task.current_step_id)
                if current is not None and current.status in (
                    STEP_STATUS_PENDING,
                    STEP_STATUS_ACTIVE,
                ):
                    repo.update_step_status(current.id, STEP_STATUS_SKIPPED)
            updated = repo.update_task(
                task.id,
                expected_version=task.version,
                status=TASK_STATUS_ABANDONED,
                abandoned_at=beijing_now_str(),
            )
            repo.append_task_event(
                task_id=task.id,
                platform_user_id=platform_user_id,
                event_type=_EVENT_TASK_ABANDONED,
                payload=self._payload(task_id=task.id),
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return updated

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 255:
            raise NookiDomainError("operation_id_invalid")

    @staticmethod
    def _validate_task_input(title: str, raw_goal: str) -> tuple[str, str]:
        clean_title = title.strip() if isinstance(title, str) else ""
        clean_goal = raw_goal.strip() if isinstance(raw_goal, str) else ""
        if not 1 <= len(clean_title) <= 80:
            raise NookiDomainError("task_title_invalid")
        if not 1 <= len(clean_goal) <= 500:
            raise NookiDomainError("raw_goal_invalid")
        return clean_title, clean_goal

    @classmethod
    def _validate_plan_drafts(
        cls, options: Sequence[PlanDraft]
    ) -> tuple[PlanDraft, ...]:
        if len(options) != PLAN_MODE_COUNT:
            raise NookiDomainError("plan_options_invalid")
        by_mode: dict[str, PlanDraft] = {}
        for draft in options:
            title = draft.title.strip() if isinstance(draft.title, str) else ""
            description = (
                draft.description.strip()
                if isinstance(draft.description, str)
                else draft.description
            )
            if draft.mode not in _VALID_PLAN_MODES or draft.mode in by_mode:
                raise NookiDomainError("plan_options_invalid")
            if not 1 <= len(title) <= 60:
                raise NookiDomainError("plan_options_invalid")
            if description is not None and len(description) > 300:
                raise NookiDomainError("plan_options_invalid")
            if isinstance(draft.estimated_minutes, bool) or not isinstance(
                draft.estimated_minutes, int
            ):
                raise NookiDomainError("plan_options_invalid")
            low, high = _PLAN_MINUTE_RANGES[draft.mode]
            if not low <= draft.estimated_minutes <= high:
                raise NookiDomainError("plan_options_invalid")
            by_mode[draft.mode] = PlanDraft(
                mode=draft.mode,
                title=title,
                description=description,
                estimated_minutes=draft.estimated_minutes,
            )
        if set(by_mode) != _VALID_PLAN_MODES:
            raise NookiDomainError("plan_options_invalid")
        if not (
            by_mode["tiny"].estimated_minutes
            < by_mode["light"].estimated_minutes
            < by_mode["normal"].estimated_minutes
        ):
            raise NookiDomainError("plan_options_invalid")
        return tuple(by_mode[mode] for mode in ("tiny", "light", "normal"))

    @staticmethod
    def _validate_replacement(step: StepRecord, replacement: PlanDraft) -> PlanDraft:
        title = replacement.title.strip() if isinstance(replacement.title, str) else ""
        description = (
            replacement.description.strip()
            if isinstance(replacement.description, str)
            else replacement.description
        )
        minutes = replacement.estimated_minutes
        if not 1 <= len(title) <= 60:
            raise NookiDomainError("replacement_invalid")
        if description is not None and len(description) > 300:
            raise NookiDomainError("replacement_invalid")
        if step.suggested_minutes is None:
            raise NookiDomainError("step_minutes_missing")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise NookiDomainError("replacement_invalid")
        if minutes >= step.suggested_minutes:
            raise NookiDomainError("replacement_not_smaller")
        return PlanDraft(
            mode=replacement.mode,
            title=title,
            description=description,
            estimated_minutes=minutes,
        )

    @staticmethod
    def _check_expected_version(
        task: TaskRecord, expected_version: Optional[int]
    ) -> None:
        if expected_version is not None and expected_version != task.version:
            raise NookiDomainError("task_version_conflict")

    @staticmethod
    def _require_owned_task(
        repo: TaskRepository, task_id: str, platform_user_id: str
    ) -> TaskRecord:
        task = repo.lock_task(task_id)
        if task is None or task.platform_user_id != platform_user_id:
            raise NookiDomainError("task_not_found")
        return task

    @staticmethod
    def _require_step(repo: TaskRepository, step_id: str) -> StepRecord:
        step = repo.lock_step(step_id)
        if step is None:
            raise NookiDomainError("step_not_found")
        return step

    @classmethod
    def _replay_event(
        cls,
        repo: TaskRepository,
        operation_id: str,
        expected_event_type: str,
        platform_user_id: str,
    ) -> Optional[TaskEventRecord]:
        event = repo.get_task_event_by_operation_id(operation_id)
        if event is None:
            return None
        if (
            event.event_type != expected_event_type
            or event.platform_user_id != platform_user_id
        ):
            raise NookiDomainError("operation_id_conflict")
        return event

    @classmethod
    def _step_from_event(
        cls, repo: TaskRepository, event: TaskEventRecord, platform_user_id: str
    ) -> StepRecord:
        cls._require_owned_task(repo, event.task_id, platform_user_id)
        payload = cls._decode_payload(event.payload)
        step = repo.get_step(str(payload.get("step_id") or ""))
        if step is None or step.task_id != event.task_id:
            raise NookiDomainError("idempotency_resource_missing")
        return step

    @staticmethod
    def _payload(**values: str) -> str:
        return json.dumps(values, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode_payload(payload: Optional[str]) -> dict[str, Any]:
        try:
            decoded = json.loads(payload or "{}")
        except json.JSONDecodeError as err:
            raise NookiDomainError("idempotency_resource_missing") from err
        if not isinstance(decoded, dict):
            raise NookiDomainError("idempotency_resource_missing")
        return decoded


__all__ = ["GoalBreakdownService", "PLAN_MODE_COUNT"]
