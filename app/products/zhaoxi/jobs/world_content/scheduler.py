"""独立中心单例 world-content scheduler。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import record_scheduler_heartbeat
from app.time_utils import BEIJING_TZ, beijing_naive_now
from app.products.zhaoxi.jobs.world_content.generator import FeedTextGenerator, LlmFeedTextGenerator
from app.products.zhaoxi.jobs.world_content.outbox import (
    LoggingWorldEventPublisher,
    WorldEventPublisher,
    dispatch_world_outbox_batch,
)
from app.products.zhaoxi.jobs.world_content.service import generate_ai_feed_batch
from app.products.zhaoxi.jobs.world_content.windows import FeedWindows

logger = logging.getLogger("ai4all.world_content.scheduler")


def _run_metrics(generation: Dict[str, Any], outbox: Dict[str, Any]) -> Dict[str, Any]:
    """把一轮明细压成稳定、低基数的 M3 heartbeat 指标。"""

    statuses = [
        str(item.get("status") or "unknown")
        for item in generation.get("results", [])
    ]
    claimed_statuses = {
        "published",
        "generation_failed",
        "author_inactive",
        "invalid_content",
        "author is not active",
        "slot window closed",
    }
    retry_results = [
        item
        for item in generation.get("results", [])
        if item.get("status") == "generation_failed"
    ]
    retry_scheduled = sum(
        item.get("retry_status") == "generating" for item in retry_results
    )
    retry_exhausted = sum(
        item.get("retry_status") == "skipped" for item in retry_results
    )
    return {
        "feed_slot_claimed": sum(status in claimed_statuses for status in statuses),
        "feed_slot_claim_conflict": statuses.count("already_claimed"),
        "feed_slot_skipped": int(generation.get("closed_expired") or 0)
        + sum(
            status
            in {
                "author_inactive",
                "invalid_content",
                "author is not active",
                "slot window closed",
            }
            for status in statuses
        )
        + retry_exhausted,
        "feed_retry_scheduled": retry_scheduled,
        "feed_retry_exhausted": retry_exhausted,
        "feed_published": int(generation.get("published") or 0),
        "outbox_claimed": int(outbox.get("claimed") or 0),
        "outbox_delivered": int(outbox.get("delivered") or 0),
        "outbox_failed": int(outbox.get("failed") or 0),
        "outbox_dead": int((outbox.get("queue") or {}).get("dead") or 0),
        "outbox_pending": int((outbox.get("queue") or {}).get("pending") or 0),
        "outbox_processing": int((outbox.get("queue") or {}).get("processing") or 0),
        "outbox_lag_seconds": int(
            (outbox.get("queue") or {}).get("oldest_undelivered_lag_seconds") or 0
        ),
    }


class WorldContentScheduler:
    """串行编排 AI Feed 生成与可多 worker 的 outbox claim。"""

    def __init__(
        self,
        *,
        enabled: bool,
        windows: FeedWindows,
        interval_seconds: float,
        batch_size: int,
        claim_lease_seconds: int,
        retry_max_attempts: int,
        retry_base_seconds: int,
        outbox_batch_size: int,
        outbox_claim_lease_seconds: int,
        outbox_max_attempts: int,
        generator: Optional[FeedTextGenerator] = None,
        publisher: Optional[WorldEventPublisher] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.windows = windows
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.batch_size = max(1, int(batch_size))
        self.claim_lease_seconds = max(1, int(claim_lease_seconds))
        self.retry_max_attempts = max(1, int(retry_max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.outbox_batch_size = max(1, int(outbox_batch_size))
        self.outbox_claim_lease_seconds = max(1, int(outbox_claim_lease_seconds))
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.generator = generator or LlmFeedTextGenerator()
        self.publisher = publisher or LoggingWorldEventPublisher()
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self._generation_after_universe_id: Optional[str] = None
        self._generation_slot_key: Optional[tuple[str, str]] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = now or beijing_naive_now()
        if current.tzinfo is not None:
            current = current.astimezone(BEIJING_TZ).replace(tzinfo=None)
        if not self.enabled:
            result = {"status": "disabled", "generation": None, "outbox": None}
            self.last_run = result
            return result
        resolved_slot = self.windows.resolve(current)
        slot_key = (
            (resolved_slot.local_date, resolved_slot.name) if resolved_slot else None
        )
        if slot_key != self._generation_slot_key:
            self._generation_after_universe_id = None
            self._generation_slot_key = slot_key
        generation = await asyncio.to_thread(
            generate_ai_feed_batch,
            now=current,
            windows=self.windows,
            generator=self.generator,
            batch_size=self.batch_size,
            claim_lease_seconds=self.claim_lease_seconds,
            retry_max_attempts=self.retry_max_attempts,
            retry_base_seconds=self.retry_base_seconds,
            after_universe_id=self._generation_after_universe_id,
        )
        self._generation_after_universe_id = generation.get(
            "next_after_universe_id"
        )
        outbox = await asyncio.to_thread(
            dispatch_world_outbox_batch,
            now=current,
            publisher=self.publisher,
            batch_size=self.outbox_batch_size,
            claim_lease_seconds=self.outbox_claim_lease_seconds,
            max_attempts=self.outbox_max_attempts,
            retry_base_seconds=self.retry_base_seconds,
        )
        result = {
            "status": "ok",
            "generation": generation,
            "outbox": outbox,
            "metrics": _run_metrics(generation, outbox),
        }
        self.last_run = result
        self.last_error = None
        return result

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(), name="ai4all-world-content-scheduler"
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
            except Exception as err:  # noqa: BLE001 — loop 必须持续并通过 heartbeat 报错
                self.last_error = str(err)
                self._heartbeat("error", str(err))
                logger.exception("world content scheduler run failed: %s", err)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _heartbeat(self, status: str, error: Optional[str] = None) -> None:
        try:
            record_scheduler_heartbeat(
                service="world_content_scheduler",
                status=status,
                error=error,
                metadata={
                    "enabled": self.enabled,
                    "interval_seconds": self.interval_seconds,
                    "batch_size": self.batch_size,
                    "outbox_batch_size": self.outbox_batch_size,
                    "last_run_metrics": (
                        self.last_run.get("metrics") if self.last_run else None
                    ),
                },
            )
        except Exception as err:
            logger.warning("world content heartbeat write failed: %s", err)
