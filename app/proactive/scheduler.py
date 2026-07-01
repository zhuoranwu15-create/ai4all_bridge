"""Compatibility exports for the proactive scheduler.

The scheduler implementation lives in app.proactive.orchestration.scheduler
after the proactive package split. Keep this module so older tests and scripts
can import app.proactive.scheduler without knowing the internal layout.
"""

from app.proactive.orchestration.scheduler import (  # noqa: F401
    ProactiveScheduler,
    get_proactive_scheduler,
    run_proactive_scheduler_once,
)
