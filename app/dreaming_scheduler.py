import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.session_lifecycle import run_daily_dreaming_scan


logger = logging.getLogger("ai4all.dreaming.scheduler")

# Minimum sleep floor so the loop never busy-waits if clock/config is odd.
_MIN_SLEEP_SECONDS = 60.0


def _seconds_until_next_window(now: datetime, *, start_hour: int = 4) -> float:
    """Return seconds until the next business-day boundary (start_hour)."""
    today_boundary = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if now >= today_boundary:
        next_boundary = today_boundary + timedelta(days=1)
    else:
        next_boundary = today_boundary
    return max((next_boundary - now).total_seconds(), _MIN_SLEEP_SECONDS)


class DreamingScheduler:
    """Scheduler for daily Dreaming scans. Wakes once per day at start_hour."""

    def __init__(
        self,
        *,
        batch_size: int = 100,
        start_hour: int = 4,
    ) -> None:
        self.batch_size = max(int(batch_size), 1)
        self.start_hour = max(0, min(int(start_hour), 23))
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        now = datetime.now()
        return {
            "running": self.is_running,
            "start_hour": self.start_hour,
            "batch_size": self.batch_size,
            "seconds_until_next_window": round(_seconds_until_next_window(now, start_hour=self.start_hour)),
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
            sleep_seconds = _seconds_until_next_window(datetime.now(), start_hour=self.start_hour)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=sleep_seconds,
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
) -> DreamingScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = DreamingScheduler(
            batch_size=batch_size,
            start_hour=start_hour,
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
    scheduler = DreamingScheduler(batch_size=batch_size)
    return await scheduler.run_once(now=now)
