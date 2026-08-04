"""运行鸣蝉独立中心 lifecycle/mailbox/visit/wish scheduler。"""
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
from app.bootstrap.product_registry import MINGCHAN_APP_ID, PRODUCTION_PRODUCT_REGISTRY  # noqa: E402
from app.products.mingchan.jobs.world_lifecycle.scheduler import WorldLifecycleScheduler  # noqa: E402

logger = logging.getLogger("ai4all.mingchan.world_lifecycle.process")


async def main() -> None:
    try:
        PRODUCTION_PRODUCT_REGISTRY.require_enabled(MINGCHAN_APP_ID)
    except ValueError:
        logger.warning("mingchan world-lifecycle scheduler disabled by product registry")
        return
    lifecycle_enabled = settings.mingchan_lifecycle_evaluation_enabled
    mailbox_enabled = settings.mingchan_mailbox_enabled
    visits_enabled = settings.mingchan_visits_enabled
    wishes_enabled = mailbox_enabled
    if not settings.has_central_role:
        raise RuntimeError("world-lifecycle scheduler must run on a central-capable process")
    init_db()
    configure_error_log_alerting(settings)
    verify_host_timezone()
    scheduler = WorldLifecycleScheduler(
        enabled=lifecycle_enabled,
        mailbox_enabled=mailbox_enabled,
        visits_enabled=visits_enabled,
        wishes_enabled=wishes_enabled,
        maintenance_enabled=True,
        interval_seconds=(
            settings.mingchan_lifecycle_scheduler_interval_seconds
        ),
        batch_size=settings.mingchan_lifecycle_scheduler_batch_size,
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
