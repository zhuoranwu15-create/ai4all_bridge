import asyncio
import signal
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.products.zhaoxi.proactive.orchestration.scheduler import ProactiveScheduler  # noqa: E402
from app.products.zhaoxi.jobs.dreaming.scheduler import DreamingScheduler  # noqa: E402
from app.products.zhaoxi.application import (  # noqa: E402
    build_companion_world_memory_sink,
    compact_companion_world_memory_batch,
)
from app.platform.media.reclaim import reclaim_orphan_media_batch  # noqa: E402
from app.products.zhaoxi.jobs.media_moderation import review_pending_media_job  # noqa: E402
from app.platform.observability.alerting import configure_error_log_alerting  # noqa: E402
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
        # v1.5 D-10 孤儿媒体回收：媒体文件只落在中心机磁盘，纯 node 跑会删掉库行却删不到文件，
        # 反而留下无主文件，所以只在具备中心能力的进程里挂载。函数自身按小时节流。
        reclaim_orphan_media=(
            reclaim_orphan_media_batch if settings.has_central_role else None
        ),
        # v1.5 S4 图片机审：待审队列是中心库里的 App 内容，且送审 URL 必须指向中心机的媒体读
        # 端点，纯 node 挂它只会签出自己取不到的地址，所以同样只在中心角色上挂。
        review_pending_media=(
            review_pending_media_job if settings.has_central_role else None
        ),
    )
    # 统一编排 P4：4 点 dreaming 扫描默认挂在本单例进程（proactive scheduler 已是单例 asyncio
    # 循环），与 FastAPI in-process DREAMING_SCHEDULER_ENABLED（默认关）互斥，避免多实例重复扫描。
    # 节点角色沿用 main.py 逻辑：具备中心能力的机器扫全量（node_id=None），纯 node 只扫自身账号。
    dreaming_scheduler = None
    if getattr(settings, "proactive_dreaming_scheduler_enabled", True):
        daily_scan_node_id = None if settings.has_central_role else (settings.node_id or None)
        dreaming_scheduler = DreamingScheduler(
            batch_size=settings.dreaming_scheduler_batch_size,
            start_hour=settings.conversation_session_business_day_start_hour,
            node_id=daily_scan_node_id,
            memory_sink=build_companion_world_memory_sink(),
            memory_compactor=(
                compact_companion_world_memory_batch
                if settings.has_central_role
                else None
            ),
        )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    scheduler.start()
    if dreaming_scheduler is not None:
        dreaming_scheduler.start()
    try:
        await stop_event.wait()
    finally:
        if dreaming_scheduler is not None:
            await dreaming_scheduler.stop()
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
