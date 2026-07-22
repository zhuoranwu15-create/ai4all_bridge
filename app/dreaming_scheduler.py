import asyncio
import logging
from datetime import datetime, timedelta

from app.time_utils import beijing_now
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.session_lifecycle import run_daily_dreaming_scan

if TYPE_CHECKING:
    from app.agent_runtime.ports import MemorySink


logger = logging.getLogger("ai4all.dreaming.scheduler")

# Minimum sleep floor so the loop never busy-waits if clock/config is odd.
_MIN_SLEEP_SECONDS = 60.0
_IDLE_HEARTBEAT_INTERVAL_SECONDS = 300.0


def _raw_seconds_until_next_window(now: datetime, *, start_hour: int = 4) -> float:
    today_boundary = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if now >= today_boundary:
        next_boundary = today_boundary + timedelta(days=1)
    else:
        next_boundary = today_boundary
    return max((next_boundary - now).total_seconds(), 0.0)


def _seconds_until_next_window(now: datetime, *, start_hour: int = 4) -> float:
    """Return seconds until the next business-day boundary (start_hour)."""
    return max(_raw_seconds_until_next_window(now, start_hour=start_hour), _MIN_SLEEP_SECONDS)


class DreamingScheduler:
    """Scheduler for daily Dreaming scans. Wakes once per day at start_hour."""

    def __init__(
        self,
        *,
        batch_size: int = 100,
        start_hour: int = 4,
        node_id: Optional[str] = None,
        memory_sink: Optional["MemorySink"] = None,
        memory_compactor: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        self.batch_size = max(int(batch_size), 1)
        self.start_hour = max(0, min(int(start_hour), 23))
        # 厚节点改造 P4：节点角色时只扫本节点账号
        self.node_id: Optional[str] = node_id or None
        self.memory_sink = memory_sink
        self.memory_compactor = memory_compactor
        self._memory_compact_cursor: Optional[str] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        now = beijing_now()
        return {
            "running": self.is_running,
            "start_hour": self.start_hour,
            "batch_size": self.batch_size,
            "seconds_until_next_window": round(
                _seconds_until_next_window(now, start_hour=self.start_hour)
            ),
            "last_run": self.last_run,
            "last_error": self.last_error,
            "memory_compact_cursor": self._memory_compact_cursor,
        }

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            run_daily_dreaming_scan,
            now=now,
            limit=self.batch_size,
            node_id=self.node_id,
            memory_sink=self.memory_sink,
        )
        if self.memory_compactor is not None:
            compact_result = await asyncio.to_thread(
                self.memory_compactor,
                limit=self.batch_size,
                after_universe_id=self._memory_compact_cursor,
            )
            self._memory_compact_cursor = compact_result.get("next_cursor")
            result["memory_compaction"] = compact_result
        self.last_run = result
        self.last_error = None
        return result

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="ai4all-dreaming-scheduler")

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
                await self.run_once()
                self._record_heartbeat(status="ok")
            except Exception as err:
                self.last_error = str(err)
                self._record_heartbeat(status="error", error=str(err))
                logger.exception("dreaming scheduler run failed: %s", err)
                await self._sleep_until_next_window(
                    heartbeat_status="error",
                    heartbeat_error=str(err),
                )
                continue
            await self._sleep_until_next_window(heartbeat_status="idle")

    async def _sleep_until_next_window(
        self,
        *,
        heartbeat_status: str,
        heartbeat_error: Optional[str] = None,
    ) -> None:
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            sleep_seconds = _raw_seconds_until_next_window(
                beijing_now(),
                start_hour=self.start_hour,
            )
            if sleep_seconds <= 0:
                return
            timeout = min(sleep_seconds, _IDLE_HEARTBEAT_INTERVAL_SECONDS)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=timeout,
                )
                return
            except asyncio.TimeoutError:
                if sleep_seconds <= _IDLE_HEARTBEAT_INTERVAL_SECONDS:
                    return
                self._record_heartbeat(status=heartbeat_status, error=heartbeat_error)

    def _record_heartbeat(self, *, status: str, error: Optional[str] = None) -> None:
        try:
            record_scheduler_heartbeat(
                service="dreaming_scheduler",
                status=status,
                error=error,
                metadata={
                    "start_hour": self.start_hour,
                    "batch_size": self.batch_size,
                },
            )
        except Exception as err:
            logger.warning("dreaming scheduler heartbeat write failed: %s", err)


_scheduler: Optional[DreamingScheduler] = None


def get_dreaming_scheduler() -> Optional[DreamingScheduler]:
    return _scheduler


def start_dreaming_scheduler(
    *,
    batch_size: int,
    start_hour: int = 4,
    node_id: Optional[str] = None,
    memory_sink: Optional["MemorySink"] = None,
    memory_compactor: Optional[Callable[..., Dict[str, Any]]] = None,
) -> DreamingScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DreamingScheduler(
            batch_size=batch_size,
            start_hour=start_hour,
            node_id=node_id,
            memory_sink=memory_sink,
            memory_compactor=memory_compactor,
        )
    if not _scheduler.is_running:
        _scheduler.start()
    return _scheduler


async def stop_dreaming_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


async def run_dreaming_scheduler_once(
    *,
    batch_size: int,
    node_id: Optional[str] = None,
    now: Optional[datetime] = None,
    memory_sink: Optional["MemorySink"] = None,
    memory_compactor: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    scheduler = DreamingScheduler(
        batch_size=batch_size,
        node_id=node_id,
        memory_sink=memory_sink,
        memory_compactor=memory_compactor,
    )
    return await scheduler.run_once(now=now)
