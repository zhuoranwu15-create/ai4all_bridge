"""M4 独立中心 lifecycle evaluation + mailbox maintenance scheduler。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.platform.media.reclaim import reclaim_orphan_media_batch
from app.products.mingchan.application.lifecycle import (
    CompanionWorldLifecycleService,
    build_lifecycle_policy,
)
from app.products.mingchan.application.mailbox import CompanionWorldMailboxService
from app.products.mingchan.application.resident_wishes import (
    CompanionWorldResidentWishService,
)
from app.products.mingchan.application.visits import CompanionWorldVisitService
from app.products.mingchan.infrastructure.persistence.notifications import (
    cleanup_app_notifications_batch,
)
from app.products.mingchan.jobs.media_moderation import review_pending_media_job
from app.time_utils import BEIJING_TZ, beijing_naive_now

logger = logging.getLogger("ai4all.world_lifecycle.scheduler")


class WorldLifecycleScheduler:
    """运行鸣蝉 lifecycle、mailbox、visit、wish 与维护型有界步骤。"""

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
        wishes_enabled: bool = False,
        wish_service: Optional[CompanionWorldResidentWishService] = None,
        maintenance_enabled: bool = False,
        cleanup_notifications: Callable[
            ..., Dict[str, Any]
        ] = cleanup_app_notifications_batch,
        reclaim_media: Callable[..., Dict[str, Any]] = reclaim_orphan_media_batch,
        review_pending_media: Callable[..., Dict[str, Any]] = review_pending_media_job,
    ) -> None:
        self.enabled = bool(enabled)
        self.mailbox_enabled = bool(mailbox_enabled)
        self.visits_enabled = bool(visits_enabled)
        self.wishes_enabled = bool(wishes_enabled)
        self.maintenance_enabled = bool(maintenance_enabled)
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
        self.wish_service = wish_service or (
            CompanionWorldResidentWishService() if self.wishes_enabled else None
        )
        self._cleanup_notifications = cleanup_notifications
        self._reclaim_media = reclaim_media
        self._review_pending_media = review_pending_media
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
        if (
            not self.enabled
            and not self.mailbox_enabled
            and not self.visits_enabled
            and not self.wishes_enabled
            and not self.maintenance_enabled
        ):
            result = {
                "status": "disabled",
                "metrics": None,
                "mailbox_metrics": None,
                "visit_metrics": None,
                "wish_metrics": None,
                "maintenance_metrics": None,
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
        wishes = None
        if self.wishes_enabled:
            if self.wish_service is None:
                raise RuntimeError("resident wish service is unavailable")
            wishes = await asyncio.to_thread(
                self.wish_service.maintain_batch,
                now=current,
                batch_size=self.batch_size,
            )
        maintenance = None
        step_errors: Dict[str, str] = {}
        if self.maintenance_enabled:
            now_text = current.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

            async def _maintenance_step(
                name: str, function: Callable[..., Dict[str, Any]], **kwargs: Any
            ) -> Dict[str, Any]:
                try:
                    return await asyncio.to_thread(function, **kwargs)
                except Exception as err:  # noqa: BLE001 - 单个维护步骤不得拖垮整个 tick
                    logger.exception("world lifecycle maintenance %s failed: %s", name, err)
                    step_errors[name] = str(err)
                    return {}

            notification_cleanup = await _maintenance_step(
                "notification_cleanup",
                self._cleanup_notifications,
                now=now_text,
                limit=self.batch_size,
            )
            media_reclaim = await _maintenance_step(
                "media_reclaim",
                self._reclaim_media,
                now=now_text,
                limit=self.batch_size,
            )
            media_moderation = await _maintenance_step(
                "media_moderation",
                self._review_pending_media,
                limit=self.batch_size,
            )
            maintenance = {
                "notification_cleanup": notification_cleanup,
                "media_reclaim": media_reclaim,
                "media_moderation": media_moderation,
            }
        result = {
            "status": "ok" if not step_errors else "partial_error",
            "metrics": evaluation.get("metrics") if evaluation else None,
            "mailbox_metrics": mailbox.get("metrics") if mailbox else None,
            "visit_metrics": visits.get("metrics") if visits else None,
            "wish_metrics": wishes.get("metrics") if wishes else None,
            "maintenance_metrics": maintenance,
            "results": evaluation.get("results") if evaluation else [],
            "mailbox_results": mailbox.get("results") if mailbox else [],
            "wish_results": wishes.get("results") if wishes else [],
            "next_after_resident_id": self._after_resident_id,
            "next_after_universe_id": self._after_universe_id,
            "errors": step_errors or None,
        }
        self.last_run = result
        self.last_error = (
            None
            if not step_errors
            else "; ".join(f"{name}: {message}" for name, message in step_errors.items())
        )
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
                    "wishes_enabled": self.wishes_enabled,
                    "maintenance_enabled": self.maintenance_enabled,
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
                    "last_run_wish_metrics": (
                        self.last_run.get("wish_metrics") if self.last_run else None
                    ),
                    "last_run_maintenance_metrics": (
                        self.last_run.get("maintenance_metrics")
                        if self.last_run
                        else None
                    ),
                },
            )
        except Exception as err:  # noqa: BLE001 - heartbeat 不得杀 scheduler
            logger.warning("world lifecycle heartbeat write failed: %s", err)


__all__ = ["WorldLifecycleScheduler"]
