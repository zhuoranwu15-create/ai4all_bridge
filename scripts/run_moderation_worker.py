import signal
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.moderation.worker import run_once  # noqa: E402


def main() -> None:
    # MODERATION_WORKER_ENABLED 控制是否真正启动 worker 进程；Phase A 默认 false。
    # 需要临时跑机器审核时显式置 true（run_once 仍可被测试直接调用，不受此开关影响）。
    if not bool(getattr(settings, "moderation_worker_enabled", False)):
        print(
            "moderation worker is disabled (set MODERATION_WORKER_ENABLED=true to run); exiting.",
            flush=True,
        )
        return

    init_db()
    stop = False

    def _stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    interval = float(settings.moderation_worker_interval_seconds)
    while not stop:
        run_once(batch_size=int(settings.moderation_worker_batch_size))
        time.sleep(max(0.1, interval))


if __name__ == "__main__":
    main()
