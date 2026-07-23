"""运行 Companion World M4 独立中心 lifecycle shadow scheduler。"""
import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.alerting import configure_error_log_alerting  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.time_utils import verify_host_timezone  # noqa: E402
from app.world_lifecycle.scheduler import WorldLifecycleScheduler  # noqa: E402

logger = logging.getLogger("ai4all.world_lifecycle.process")


async def main() -> None:
    if not settings.companion_world_lifecycle_evaluation_enabled:
        logger.warning(
            "world-lifecycle scheduler disabled by "
            "COMPANION_WORLD_LIFECYCLE_EVALUATION_ENABLED=false"
        )
        return
    if not settings.has_central_role:
        raise RuntimeError("world-lifecycle scheduler must run on a central-capable process")
    init_db()
    configure_error_log_alerting(settings)
    verify_host_timezone()
    scheduler = WorldLifecycleScheduler(
        enabled=True,
        interval_seconds=(
            settings.companion_world_lifecycle_scheduler_interval_seconds
        ),
        batch_size=settings.companion_world_lifecycle_scheduler_batch_size,
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
