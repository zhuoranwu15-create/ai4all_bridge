"""M4 独立中心 lifecycle evaluation + mailbox maintenance scheduler。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.platform.companion_world_lifecycle import (
    CompanionWorldLifecycleService,
    build_lifecycle_policy,
)
from app.platform.companion_world_mailbox import CompanionWorldMailboxService
from app.platform.companion_world_visits import CompanionWorldVisitService
from app.time_utils import BEIJING_TZ, beijing_naive_now

logger = logging.getLogger("ai4all.world_lifecycle.scheduler")


class WorldLifecycleScheduler:
    """运行 lifecycle/mailbox/visit expiry 三个独立、可关闭的有界步骤。"""

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: float,
        batch_size: int,
        service: Optional[CompanionWorldLifecycleService] = None,
        mailbox_enabled: bool = False,
        mailbox_service: Optional[CompanionWorldMailboxService] = None,
        visits_enabled: bool = False,
        visits_service: Optional[CompanionWorldVisitService] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.mailbox_enabled = bool(mailbox_enabled)
        self.visits_enabled = bool(visits_enabled)
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.batch_size = max(1, min(int(batch_size), 500))
        self.service = service or (
            CompanionWorldLifecycleService(policy=build_lifecycle_policy())
            if self.enabled
            else None
        )
        self.mailbox_service = mailbox_service or (
            CompanionWorldMailboxService() if self.mailbox_enabled else None
        )
        self.visits_service = visits_service or (
            CompanionWorldVisitService() if self.visits_enabled else None
        )
        self._after_resident_id: Optional[str] = None
        self._after_universe_id: Optional[str] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = now or beijing_naive_now()
        if current.tzinfo is not None:
            current = current.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if not self.enabled and not self.mailbox_enabled and not self.visits_enabled:
            result = {
                "status": "disabled",
                "metrics": None,
                "mailbox_metrics": None,
                "visit_metrics": None,
                "next_after_resident_id": None,
                "next_after_universe_id": None,
            }
            self.last_run = result
            return result
        evaluation = None
        if self.enabled:
            if self.service is None:
                raise RuntimeError("lifecycle service is unavailable")
            evaluation = await asyncio.to_thread(
                self.service.evaluate_batch,
                now=current,
                after_resident_id=self._after_resident_id,
                batch_size=self.batch_size,
            )
            self._after_resident_id = evaluation.get("next_after_resident_id")
        mailbox = None
        if self.mailbox_enabled:
            if self.mailbox_service is None:
                raise RuntimeError("mailbox service is unavailable")
            mailbox = await asyncio.to_thread(
                self.mailbox_service.maintain_batch,
                now=current,
                after_universe_id=self._after_universe_id,
                batch_size=self.batch_size,
            )
            self._after_universe_id = mailbox.get("next_after_universe_id")
        visits = None
        if self.visits_enabled:
            if self.visits_service is None:
                raise RuntimeError("visit expiry service is unavailable")
            visits = await asyncio.to_thread(
                self.visits_service.maintain_expiry_batch,
                now=current,
                batch_size=self.batch_size,
            )
        result = {
            "status": "ok",
            "metrics": evaluation.get("metrics") if evaluation else None,
            "mailbox_metrics": mailbox.get("metrics") if mailbox else None,
            "visit_metrics": visits.get("metrics") if visits else None,
            "results": evaluation.get("results") if evaluation else [],
            "mailbox_results": mailbox.get("results") if mailbox else [],
            "next_after_resident_id": self._after_resident_id,
            "next_after_universe_id": self._after_universe_id,
        }
        self.last_run = result
        self.last_error = None
        return result

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(), name="ai4all-world-lifecycle-scheduler"
        )

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
                self._heartbeat("running")
                await self.run_once()
                self._heartbeat("ok")
            except Exception as err:  # noqa: BLE001 - scheduler 必须持续并上报
                self.last_error = str(err)
                self._heartbeat("error", str(err))
                logger.exception("world lifecycle scheduler run failed: %s", err)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _heartbeat(self, status: str, error: Optional[str] = None) -> None:
        """写入低基数字段 heartbeat，不落 resident/account/evidence 明细。"""
        try:
            record_scheduler_heartbeat(
                service="world_lifecycle_scheduler",
                status=status,
                error=error,
                metadata={
                    "enabled": self.enabled,
                    "mailbox_enabled": self.mailbox_enabled,
                    "visits_enabled": self.visits_enabled,
                    "interval_seconds": self.interval_seconds,
                    "batch_size": self.batch_size,
                    "last_run_metrics": (
                        self.last_run.get("metrics") if self.last_run else None
                    ),
                    "last_run_mailbox_metrics": (
                        self.last_run.get("mailbox_metrics")
                        if self.last_run
                        else None
                    ),
                    "last_run_visit_metrics": (
                        self.last_run.get("visit_metrics")
                        if self.last_run
                        else None
                    ),
                },
            )
        except Exception as err:  # noqa: BLE001 - heartbeat 不得杀 scheduler
            logger.warning("world lifecycle heartbeat write failed: %s", err)


__all__ = ["WorldLifecycleScheduler"]
