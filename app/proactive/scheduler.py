import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.proactive.commitments import dispatch_due_commitments
from app.proactive.content_invitations import expire_stale_content_invitations
from app.proactive.reactivation import dispatch_due_reactivation_candidates
from app.proactive.reminders import dispatch_due_reminders
from app.proactive.state import (
    DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    scan_due_proactive_account_checks,
)
from app.db import record_scheduler_heartbeat
from app.time_utils import beijing_naive_now


DispatchDueReminders = Callable[..., List[Dict[str, Any]]]
DispatchDueCommitments = Callable[..., List[Dict[str, Any]]]
DispatchDueReactivation = Callable[..., List[Dict[str, Any]]]
ExpireContentInvitations = Callable[..., List[Dict[str, Any]]]
ScanDueAccountChecks = Callable[..., List[Dict[str, Any]]]

logger = logging.getLogger("ai4all.proactive.scheduler")


class ProactiveScheduler:
    """System-level scheduler for cheap proactive work scans."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        batch_size: int,
        bypass_quiet_hours: bool = False,
        planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
        node_id: Optional[str] = None,
        dispatch_reminders: DispatchDueReminders = dispatch_due_reminders,
        dispatch_commitments: DispatchDueCommitments = dispatch_due_commitments,
        dispatch_reactivation: DispatchDueReactivation = dispatch_due_reactivation_candidates,
        expire_content_invitations: ExpireContentInvitations = expire_stale_content_invitations,
        scan_account_checks: ScanDueAccountChecks = scan_due_proactive_account_checks,
    ) -> None:
        self.interval_seconds = max(float(interval_seconds), 1.0)
        self.batch_size = max(int(batch_size), 1)
        self.bypass_quiet_hours = bypass_quiet_hours
        self.planning_interval_seconds = max(int(planning_interval_seconds), 1)
        # 厚节点改造 P4：节点角色时只扫本节点账号（assigned_node_id = node_id）
        self.node_id: Optional[str] = node_id or None
        self._dispatch_reminders = dispatch_reminders
        self._dispatch_commitments = dispatch_commitments
        self._dispatch_reactivation = dispatch_reactivation
        self._expire_content_invitations = expire_content_invitations
        self._scan_account_checks = scan_account_checks
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "interval_seconds": self.interval_seconds,
            "batch_size": self.batch_size,
            "bypass_quiet_hours": self.bypass_quiet_hours,
            "planning_interval_seconds": self.planning_interval_seconds,
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        started_at = beijing_naive_now()
        current = now or started_at
        # 各步骤相互隔离：单个 dispatcher 抛错只记录并继续，不再让前面的步骤
        # （如 reminders）持续失败时把后面的 reactivation/commitment 发送整轮饿死。
        step_errors: Dict[str, str] = {}

        async def _step(name: str, fn: Callable[..., List[Dict[str, Any]]], **kwargs: Any) -> List[Dict[str, Any]]:
            try:
                return await asyncio.to_thread(fn, **kwargs)
            except Exception as err:  # noqa: BLE001 — 步骤级隔离，单步失败不拖垮整轮
                logger.exception("proactive scheduler step %s failed: %s", name, err)
                step_errors[name] = str(err)
                return []

        reminder_results = await _step(
            "reminders",
            self._dispatch_reminders,
            now=current,
            limit=self.batch_size,
            bypass_quiet_hours=self.bypass_quiet_hours,
            node_id=self.node_id,
        )
        commitment_results = await _step(
            "commitments",
            self._dispatch_commitments,
            now=current,
            limit=self.batch_size,
            bypass_quiet_hours=self.bypass_quiet_hours,
            node_id=self.node_id,
        )
        account_results = await _step(
            "account_checks",
            self._scan_account_checks,
            now=current,
            limit=self.batch_size,
            planning_interval_seconds=self.planning_interval_seconds,
            node_id=self.node_id,
        )
        reactivation_results = await _step(
            "reactivation",
            self._dispatch_reactivation,
            now=current,
            limit=self.batch_size,
            node_id=self.node_id,
        )
        expired_content_results = await _step(
            "expire_content_invitations",
            self._expire_content_invitations,
            now=current,
            limit=self.batch_size,
        )
        finished_at = beijing_naive_now()
        result = {
            "status": "ok" if not step_errors else "partial_error",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "reminder_count": len(reminder_results),
            "reminders": reminder_results,
            "commitment_count": len(commitment_results),
            "commitments": commitment_results,
            "account_check_count": len(account_results),
            "account_checks": account_results,
            "reactivation_count": len(reactivation_results),
            "reactivations": reactivation_results,
            "expired_content_invitation_count": len(expired_content_results),
            "expired_content_invitations": expired_content_results,
            "errors": step_errors or None,
        }
        self.last_run = result
        # 部分步骤失败时保留错误供 heartbeat/监控感知，不再静默吞掉。
        self.last_error = (
            None
            if not step_errors
            else "; ".join(f"{name}: {msg}" for name, msg in step_errors.items())
        )
        return result

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="ai4all-proactive-scheduler")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
            self._stop_event = None

    async def _run_loop(self) -> None:
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            try:
                self._record_heartbeat(status="running")
                run_result = await self.run_once()
                # run_once 现在做步骤级隔离，不再抛步骤错误；部分失败时仍要让
                # heartbeat 反映出来，否则监控会把"部分步骤一直失败"当成健康。
                if run_result.get("errors"):
                    self._record_heartbeat(status="error", error=self.last_error)
                else:
                    self._record_heartbeat(status="ok")
            except Exception as err:
                self.last_error = str(err)
                self._record_heartbeat(status="error", error=str(err))
                logger.exception("proactive scheduler run failed: %s", err)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _record_heartbeat(self, *, status: str, error: Optional[str] = None) -> None:
        try:
            record_scheduler_heartbeat(
                service="proactive_scheduler",
                status=status,
                error=error,
                metadata={
                    "interval_seconds": self.interval_seconds,
                    "batch_size": self.batch_size,
                    "bypass_quiet_hours": self.bypass_quiet_hours,
                    "planning_interval_seconds": self.planning_interval_seconds,
                },
            )
        except Exception as err:
            logger.warning("proactive scheduler heartbeat write failed: %s", err)


_scheduler: Optional[ProactiveScheduler] = None


def get_proactive_scheduler() -> Optional[ProactiveScheduler]:
    return _scheduler


def start_proactive_scheduler(
    *,
    interval_seconds: float,
    batch_size: int,
    bypass_quiet_hours: bool = False,
    planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    node_id: Optional[str] = None,
) -> ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ProactiveScheduler(
            interval_seconds=interval_seconds,
            batch_size=batch_size,
            bypass_quiet_hours=bypass_quiet_hours,
            planning_interval_seconds=planning_interval_seconds,
            node_id=node_id,
        )
    if not _scheduler.is_running:
        _scheduler.start()
    return _scheduler


async def stop_proactive_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


async def run_proactive_scheduler_once(
    *,
    batch_size: int,
    bypass_quiet_hours: bool = False,
    planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    node_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    scheduler = ProactiveScheduler(
        interval_seconds=60,
        batch_size=batch_size,
        bypass_quiet_hours=bypass_quiet_hours,
        planning_interval_seconds=planning_interval_seconds,
        node_id=node_id,
    )
    return await scheduler.run_once(now=now)
