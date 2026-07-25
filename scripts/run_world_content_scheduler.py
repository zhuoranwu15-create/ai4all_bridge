"""运行 Companion World 独立中心 world-content scheduler。"""
import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.platform.observability.alerting import configure_error_log_alerting  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.time_utils import verify_host_timezone  # noqa: E402
from app.products.zhaoxi.jobs.world_content import FeedWindows, WorldContentScheduler  # noqa: E402

logger = logging.getLogger("ai4all.world_content.process")


async def main() -> None:
    if not settings.companion_world_feed_enabled:
        logger.warning("world-content scheduler disabled by COMPANION_WORLD_FEED_ENABLED=false")
        return
    if not settings.has_central_role:
        raise RuntimeError("world-content scheduler must run on a central-capable process")
    windows = FeedWindows.parse(
        morning_start=settings.companion_world_feed_morning_start,
        morning_end=settings.companion_world_feed_morning_end,
        evening_start=settings.companion_world_feed_evening_start,
        evening_end=settings.companion_world_feed_evening_end,
    )
    init_db()
    configure_error_log_alerting(settings)
    verify_host_timezone()
    scheduler = WorldContentScheduler(
        enabled=True,
        windows=windows,
        interval_seconds=settings.companion_world_feed_scheduler_interval_seconds,
        batch_size=settings.companion_world_feed_scheduler_batch_size,
        claim_lease_seconds=settings.companion_world_feed_claim_lease_seconds,
        retry_max_attempts=settings.companion_world_feed_retry_max_attempts,
        retry_base_seconds=settings.companion_world_feed_retry_base_seconds,
        outbox_batch_size=settings.companion_world_outbox_batch_size,
        outbox_claim_lease_seconds=settings.companion_world_outbox_claim_lease_seconds,
        outbox_max_attempts=settings.companion_world_outbox_max_attempts,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    scheduler.start()
    try:
        await stop_event.wait()
    finally:
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
