"""Nooki 目标拆解领域契约：DTO、异常与仓储 Protocol。

抄 app/products/zhaoxi/domain/companion_world/contracts.py 的模式：错误只带 code（不带展示文
案，由 API 层翻译），DTO 全部 frozen dataclass，仓储用 Protocol + transaction() 上下文管理器。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ContextManager, Optional, Protocol, Sequence


class NookiDomainError(Exception):
    """目标拆解领域内的状态机/校验错误；只带 code，不带展示文案。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# 任务状态机：draft（已建草稿，未生成方案）→ ready（三档方案已生成，待选）→
# active（已选方案，有一个 step 在跑）→ done/abandoned（终态）。
TASK_STATUS_DRAFT = "draft"
TASK_STATUS_READY = "ready"
TASK_STATUS_ACTIVE = "active"
TASK_STATUS_DONE = "done"
TASK_STATUS_ABANDONED = "abandoned"
_TASK_TERMINAL_STATUSES = frozenset({TASK_STATUS_DONE, TASK_STATUS_ABANDONED})

STEP_STATUS_PENDING = "pending"
STEP_STATUS_ACTIVE = "active"
STEP_STATUS_DONE = "done"
STEP_STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class TaskRecord:
    """`nooki_tasks` 一行的只读快照。"""

    id: str
    platform_user_id: str
    title: str
    raw_goal: str
    status: str
    selected_plan_id: Optional[str]
    current_step_id: Optional[str]
    source_message_id: Optional[str]
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanRecord:
    """`nooki_task_plans` 一行的只读快照（三档方案之一）。"""

    id: str
    task_id: str
    mode: str
    title: str
    description: Optional[str]
    estimated_minutes: Optional[int]
    is_selected: bool
    created_at: str


@dataclass(frozen=True)
class PlanDraft:
    """写入三档方案时的输入项（尚未持久化，无 id）。"""

    mode: str
    title: str
    description: Optional[str] = None
    estimated_minutes: Optional[int] = None


@dataclass(frozen=True)
class StepRecord:
    """`nooki_steps` 一行的只读快照。"""

    id: str
    task_id: str
    plan_id: str
    title: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskProjection:
    """`get_authoritative_state` 的只读投影：前端卡片的唯一事实源。"""

    focus_task: Optional[TaskRecord]
    plans: tuple[PlanRecord, ...] = field(default_factory=tuple)
    current_step: Optional[StepRecord] = None
    active_tasks_count: int = 0
    state_version: int = 0


class TaskRepository(Protocol):
    """目标拆解领域的仓储接口；具体 SQL 实现见 infrastructure/repositories。"""

    def transaction(self) -> ContextManager["TaskRepository"]:
        """开启一个仓储事务；写路径必须在此上下文内完成。"""
        ...

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        ...

    def get_task_by_source_message(self, source_message_id: str) -> Optional[TaskRecord]:
        ...

    def lock_task(self, task_id: str) -> Optional[TaskRecord]:
        """在事务内加行锁读取任务，供写路径校验后再更新。"""
        ...

    def create_task(
        self,
        *,
        platform_user_id: str,
        title: str,
        raw_goal: str,
        source_message_id: Optional[str],
    ) -> TaskRecord:
        ...

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        status: Optional[str] = None,
        selected_plan_id: Optional[str] = None,
        current_step_id: Optional[str] = None,
    ) -> TaskRecord:
        """乐观锁更新：`WHERE id=? AND version=?`；版本不匹配抛 `task_version_conflict`。"""
        ...

    def list_plans(self, task_id: str) -> Sequence[PlanRecord]:
        ...

    def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        ...

    def create_plans(
        self, task_id: str, drafts: Sequence[PlanDraft]
    ) -> Sequence[PlanRecord]:
        ...

    def mark_plan_selected(self, plan_id: str) -> PlanRecord:
        ...

    def get_step(self, step_id: str) -> Optional[StepRecord]:
        ...

    def lock_step(self, step_id: str) -> Optional[StepRecord]:
        """在事务内加行锁读取 step，供写路径校验后再更新。"""
        ...

    def create_step(
        self, *, task_id: str, plan_id: str, title: str, status: str
    ) -> StepRecord:
        ...

    def update_step_status(self, step_id: str, status: str) -> StepRecord:
        ...

    def append_task_event(
        self,
        *,
        task_id: str,
        platform_user_id: str,
        event_type: str,
        payload: Optional[str] = None,
        source_message_id: Optional[str] = None,
    ) -> None:
        ...

    def list_active_tasks(self, platform_user_id: str) -> Sequence[TaskRecord]:
        """返回该用户所有非终态（active/ready/draft）任务，按最近更新排序。"""
        ...


__all__ = [
    "NookiDomainError",
    "PlanDraft",
    "PlanRecord",
    "STEP_STATUS_ACTIVE",
    "STEP_STATUS_DONE",
    "STEP_STATUS_PENDING",
    "STEP_STATUS_SKIPPED",
    "StepRecord",
    "TASK_STATUS_ABANDONED",
    "TASK_STATUS_ACTIVE",
    "TASK_STATUS_DONE",
    "TASK_STATUS_DRAFT",
    "TASK_STATUS_READY",
    "TaskProjection",
    "TaskRecord",
    "TaskRepository",
]
