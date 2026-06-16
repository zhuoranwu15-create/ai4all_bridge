import asyncio
import signal
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.proactive.scheduler import ProactiveScheduler  # noqa: E402
from app.alerting import configure_error_log_alerting  # noqa: E402
from app.time_utils import verify_host_timezone  # noqa: E402


async def main() -> None:
    init_db()
    # 调度器为独立进程：自行接上 Feishu 告警 handler，并做宿主机时区第二层防御
    # （非 UTC+8 时报警但兼容继续，因调度已统一用 beijing_naive_now）。
    configure_error_log_alerting(settings)
    verify_host_timezone()
    scheduler = ProactiveScheduler(
        interval_seconds=settings.proactive_scheduler_interval_seconds,
        batch_size=settings.proactive_scheduler_batch_size,
        bypass_quiet_hours=settings.proactive_scheduler_bypass_quiet_hours,
        planning_interval_seconds=settings.proactive_planning_interval_seconds,
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
