"""运行鸣蝉 Companion World 独立中心 world-content scheduler。"""
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
from app.bootstrap.product_registry import (  # noqa: E402
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
)
from app.products.mingchan.jobs.world_content import FeedWindows, WorldContentScheduler  # noqa: E402

logger = logging.getLogger("ai4all.mingchan.world_content.process")


async def main() -> None:
    """仅在鸣蝉产品和 Feed 开关均启用时运行 central-only scheduler。"""

    try:
        PRODUCTION_PRODUCT_REGISTRY.require_enabled(MINGCHAN_APP_ID)
    except ValueError:
        logger.warning("mingchan world-content scheduler disabled by product registry")
        return
    if not settings.mingchan_feed_enabled:
        logger.warning("world-content scheduler disabled by MINGCHAN_FEED_ENABLED=false")
        return
    if not settings.has_central_role:
        raise RuntimeError("world-content scheduler must run on a central-capable process")
    windows = FeedWindows.parse(
        morning_start=settings.mingchan_feed_morning_start,
        morning_end=settings.mingchan_feed_morning_end,
        evening_start=settings.mingchan_feed_evening_start,
        evening_end=settings.mingchan_feed_evening_end,
    )
    init_db()
    configure_error_log_alerting(settings)
    verify_host_timezone()
    scheduler = WorldContentScheduler(
        enabled=True,
        windows=windows,
        interval_seconds=settings.mingchan_feed_scheduler_interval_seconds,
        batch_size=settings.mingchan_feed_scheduler_batch_size,
        claim_lease_seconds=settings.mingchan_feed_claim_lease_seconds,
        retry_max_attempts=settings.mingchan_feed_retry_max_attempts,
        retry_base_seconds=settings.mingchan_feed_retry_base_seconds,
        outbox_batch_size=settings.mingchan_outbox_batch_size,
        outbox_claim_lease_seconds=settings.mingchan_outbox_claim_lease_seconds,
        outbox_max_attempts=settings.mingchan_outbox_max_attempts,
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
