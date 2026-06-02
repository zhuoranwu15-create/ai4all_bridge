import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.session_lifecycle import run_daily_dreaming_scan


logger = logging.getLogger("ai4all.dreaming.scheduler")


class DreamingScheduler:
    """Independent scheduler for daily Dreaming scans."""

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        batch_size: int = 100,
    ) -> None:
        self.interval_seconds = max(float(interval_seconds), 30.0)
        self.batch_size = max(int(batch_size), 1)
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
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            run_daily_dreaming_scan,
            now=now,
            limit=self.batch_size,
        )
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
                service="dreaming_scheduler",
                status=status,
                error=error,
                metadata={
                    "interval_seconds": self.interval_seconds,
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
    interval_seconds: float,
    batch_size: int,
) -> DreamingScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DreamingScheduler(
            interval_seconds=interval_seconds,
            batch_size=batch_size,
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
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    scheduler = DreamingScheduler(
        interval_seconds=300,
        batch_size=batch_size,
    )
    return await scheduler.run_once(now=now)
