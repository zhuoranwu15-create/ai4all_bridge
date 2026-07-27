"""Nooki 目标拆解的 SQL adapter（SQLite/PG 共用，事务由 connect() 承载）。

抄 app/products/zhaoxi/infrastructure/repositories/companion_world.py 的
`SqlCompanionWorldRepository` 模式：绑定实例的所有写共享一条连接，
`transaction()` 未绑定时自开事务、已绑定时复用外层事务（供领域服务嵌套调用）。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

from app.db._backend import Connection, IntegrityError, is_postgres
from app.db._core import _new_id, _tx, connect
from app.time_utils import beijing_now_str
from app.products.nooki.domain.goal_breakdown.contracts import (
    NookiDomainError,
    PlanDraft,
    PlanRecord,
    StepRecord,
    TASK_STATUS_ABANDONED,
    TASK_STATUS_DONE,
    TASK_STATUS_DRAFT,
    TaskEventRecord,
    TaskRecord,
    TaskRepository,
)


def _task(row: dict) -> TaskRecord:
    return TaskRecord(
        id=str(row["id"]),
        platform_user_id=str(row["platform_user_id"]),
        title=str(row["title"]),
        raw_goal=str(row["raw_goal"]),
        status=str(row["status"]),
        selected_plan_id=row.get("selected_plan_id"),
        current_step_id=row.get("current_step_id"),
        source_message_id=row.get("source_message_id"),
        version=int(row["version"]),
        completed_at=row.get("completed_at"),
        abandoned_at=row.get("abandoned_at"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _plan(row: dict) -> PlanRecord:
    estimated_minutes = row.get("estimated_minutes")
    return PlanRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        mode=str(row["mode"]),
        title=str(row["title"]),
        description=row.get("description"),
        estimated_minutes=int(estimated_minutes) if estimated_minutes is not None else None,
        is_selected=bool(row.get("is_selected")),
        created_at=str(row["created_at"]),
    )


def _step(row: dict) -> StepRecord:
    suggested_minutes = row.get("suggested_minutes")
    return StepRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        plan_id=str(row["plan_id"]),
        title=str(row["title"]),
        description=row.get("description"),
        suggested_minutes=(
            int(suggested_minutes) if suggested_minutes is not None else None
        ),
        replaces_step_id=row.get("replaces_step_id"),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _event(row: dict) -> TaskEventRecord:
    return TaskEventRecord(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        platform_user_id=str(row["platform_user_id"]),
        event_type=str(row["event_type"]),
        payload=row.get("payload"),
        source_message_id=row.get("source_message_id"),
        operation_id=str(row["operation_id"]),
        created_at=str(row["created_at"]),
    )


class SqlTaskRepository(TaskRepository):
    """把 `TaskRepository` 端口映射到 `nooki_*` 表。"""

    def __init__(self, conn: Optional[Connection] = None) -> None:
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator["SqlTaskRepository"]:
        """开启单事务并返回绑定 repository；已有绑定时复用外层事务。"""
        if self._conn is not None:
            yield self
            return
        with connect() as conn:
            # SQLite 的 SELECT 不自动开事务；BEGIN IMMEDIATE 提供写串行与完整 rollback 边界。
            # PG 由驱动自动 BEGIN。对齐 SqlCompanionWorldRepository 的既有做法。
            if not is_postgres():
                conn.execute("BEGIN IMMEDIATE")
            yield SqlTaskRepository(conn)

    def _required_conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("operation requires repository.transaction()")
        return self._conn

    # -- task ------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with _tx(self._conn) as tx:
            row = tx.execute(
                "SELECT * FROM nooki_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _task(dict(row)) if row else None

    def get_task_by_source_message(
        self, *, platform_user_id: str, source_message_id: str
    ) -> Optional[TaskRecord]:
        with _tx(self._conn) as tx:
            row = tx.execute(
                """
                SELECT * FROM nooki_tasks
                WHERE platform_user_id = ? AND source_message_id = ?
                """,
                (platform_user_id, source_message_id),
            ).fetchone()
        return _task(dict(row)) if row else None

    def lock_task(self, task_id: str) -> Optional[TaskRecord]:
        conn = self._required_conn()
        suffix = " FOR UPDATE" if is_postgres() else ""
        row = conn.execute(
            "SELECT * FROM nooki_tasks WHERE id = ?" + suffix, (task_id,)
        ).fetchone()
        return _task(dict(row)) if row else None

    def create_task(
        self,
        *,
        platform_user_id: str,
        title: str,
        raw_goal: str,
        source_message_id: Optional[str],
    ) -> TaskRecord:
        conn = self._required_conn()
        task_id = _new_id("nktask")
        # ON CONFLICT 只在 source_message_id 非空时命中（对齐 ux_nooki_tasks_source_message
        # 的 partial unique index），是原子创建流程的 DB 层幂等兜底。
        try:
            conn.execute(
                """
                INSERT INTO nooki_tasks(
                    id, platform_user_id, title, raw_goal, status, source_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_user_id, source_message_id)
                    WHERE source_message_id IS NOT NULL DO NOTHING
                """,
                (task_id, platform_user_id, title, raw_goal, TASK_STATUS_DRAFT, source_message_id),
            )
        except IntegrityError as err:
            # 并发请求可能都通过前置读取；数据库的单未终结任务唯一索引是最终兜底。
            raise NookiDomainError("open_task_exists") from err
        if source_message_id:
            row = conn.execute(
                """
                SELECT * FROM nooki_tasks
                WHERE platform_user_id = ? AND source_message_id = ?
                """,
                (platform_user_id, source_message_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM nooki_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("task was not created")
        return _task(dict(row))

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        status: Optional[str] = None,
        selected_plan_id: Optional[str] = None,
        current_step_id: Optional[str] = None,
        completed_at: Optional[str] = None,
        abandoned_at: Optional[str] = None,
    ) -> TaskRecord:
        conn = self._required_conn()
        set_clauses = ["version = version + 1", "updated_at = ?"]
        params: list = [beijing_now_str()]
        if status is not None:
            set_clauses.append("status = ?")
            params.append(status)
        if selected_plan_id is not None:
            set_clauses.append("selected_plan_id = ?")
            params.append(selected_plan_id)
        if current_step_id is not None:
            set_clauses.append("current_step_id = ?")
            params.append(current_step_id)
        if completed_at is not None:
            set_clauses.append("completed_at = ?")
            params.append(completed_at)
        if abandoned_at is not None:
            set_clauses.append("abandoned_at = ?")
            params.append(abandoned_at)
        params.extend([task_id, expected_version])
        cursor = conn.execute(
            f"UPDATE nooki_tasks SET {', '.join(set_clauses)} WHERE id = ? AND version = ?",
            tuple(params),
        )
        if (cursor.rowcount or 0) == 0:
            raise NookiDomainError("task_version_conflict")
        row = conn.execute("SELECT * FROM nooki_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise RuntimeError("task disappeared after update")
        return _task(dict(row))

    def list_active_tasks(self, platform_user_id: str) -> Sequence[TaskRecord]:
        with _tx(self._conn) as tx:
            rows = tx.execute(
                """
                SELECT * FROM nooki_tasks
                WHERE platform_user_id = ? AND status NOT IN (?, ?)
                ORDER BY updated_at DESC, id DESC
                """,
                (platform_user_id, TASK_STATUS_DONE, TASK_STATUS_ABANDONED),
            ).fetchall()
        return tuple(_task(dict(row)) for row in rows)

    # -- plans -------------------------------------------------------------

    def list_plans(self, task_id: str) -> Sequence[PlanRecord]:
        with _tx(self._conn) as tx:
            rows = tx.execute(
                """
                SELECT * FROM nooki_task_plans
                WHERE task_id = ?
                ORDER BY CASE mode WHEN 'tiny' THEN 1 WHEN 'light' THEN 2 ELSE 3 END, id
                """,
                (task_id,),
            ).fetchall()
        return tuple(_plan(dict(row)) for row in rows)

    def get_plan(self, plan_id: str) -> Optional[PlanRecord]:
        with _tx(self._conn) as tx:
            row = tx.execute(
                "SELECT * FROM nooki_task_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return _plan(dict(row)) if row else None

    def create_plans(
        self, task_id: str, drafts: Sequence[PlanDraft]
    ) -> Sequence[PlanRecord]:
        conn = self._required_conn()
        created_ids = []
        for draft in drafts:
            plan_id = _new_id("nkplan")
            conn.execute(
                """
                INSERT INTO nooki_task_plans(id, task_id, mode, title, description, estimated_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    task_id,
                    draft.mode,
                    draft.title,
                    draft.description,
                    draft.estimated_minutes,
                ),
            )
            created_ids.append(plan_id)
        placeholders = ",".join("?" for _ in created_ids)
        rows = conn.execute(
            f"SELECT * FROM nooki_task_plans WHERE id IN ({placeholders})",
            tuple(created_ids),
        ).fetchall()
        by_id = {str(row["id"]): _plan(dict(row)) for row in rows}
        return tuple(by_id[plan_id] for plan_id in created_ids)

    def mark_plan_selected(self, plan_id: str) -> PlanRecord:
        conn = self._required_conn()
        conn.execute("UPDATE nooki_task_plans SET is_selected = 1 WHERE id = ?", (plan_id,))
        row = conn.execute(
            "SELECT * FROM nooki_task_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("plan disappeared after selection")
        return _plan(dict(row))

    # -- steps ---------------------------------------------------------------

    def get_step(self, step_id: str) -> Optional[StepRecord]:
        with _tx(self._conn) as tx:
            row = tx.execute("SELECT * FROM nooki_steps WHERE id = ?", (step_id,)).fetchone()
        return _step(dict(row)) if row else None

    def lock_step(self, step_id: str) -> Optional[StepRecord]:
        conn = self._required_conn()
        suffix = " FOR UPDATE" if is_postgres() else ""
        row = conn.execute(
            "SELECT * FROM nooki_steps WHERE id = ?" + suffix, (step_id,)
        ).fetchone()
        return _step(dict(row)) if row else None

    def create_step(
        self,
        *,
        task_id: str,
        plan_id: str,
        title: str,
        description: Optional[str],
        suggested_minutes: Optional[int],
        replaces_step_id: Optional[str],
        status: str,
    ) -> StepRecord:
        conn = self._required_conn()
        step_id = _new_id("nkstep")
        conn.execute(
            """
            INSERT INTO nooki_steps(
                id, task_id, plan_id, title, description, suggested_minutes,
                replaces_step_id, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_id,
                task_id,
                plan_id,
                title,
                description,
                suggested_minutes,
                replaces_step_id,
                status,
            ),
        )
        row = conn.execute("SELECT * FROM nooki_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None:
            raise RuntimeError("step was not created")
        return _step(dict(row))

    def update_step_status(self, step_id: str, status: str) -> StepRecord:
        conn = self._required_conn()
        conn.execute(
            "UPDATE nooki_steps SET status = ?, updated_at = ? WHERE id = ?",
            (status, beijing_now_str(), step_id),
        )
        row = conn.execute("SELECT * FROM nooki_steps WHERE id = ?", (step_id,)).fetchone()
        if row is None:
            raise RuntimeError("step disappeared after update")
        return _step(dict(row))

    # -- events ----------------------------------------------------------------

    def append_task_event(
        self,
        *,
        task_id: str,
        platform_user_id: str,
        event_type: str,
        payload: Optional[str] = None,
        source_message_id: Optional[str] = None,
        operation_id: str,
    ) -> TaskEventRecord:
        conn = self._required_conn()
        event_id = _new_id("nkevt")
        conn.execute(
            """
            INSERT INTO nooki_task_events(
                id, task_id, platform_user_id, event_type, payload,
                source_message_id, operation_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                task_id,
                platform_user_id,
                event_type,
                payload,
                source_message_id,
                operation_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM nooki_task_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("task event was not created")
        return _event(dict(row))

    def get_task_event_by_operation_id(
        self, operation_id: str
    ) -> Optional[TaskEventRecord]:
        with _tx(self._conn) as tx:
            row = tx.execute(
                "SELECT * FROM nooki_task_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return _event(dict(row)) if row else None

    def get_latest_task_id_by_source_message(
        self, *, platform_user_id: str, source_message_id: str
    ) -> Optional[str]:
        with _tx(self._conn) as tx:
            row = tx.execute(
                """
                SELECT task_id FROM nooki_task_events
                WHERE platform_user_id = ? AND source_message_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (platform_user_id, source_message_id),
            ).fetchone()
        return str(row["task_id"]) if row else None


__all__ = ["SqlTaskRepository"]
