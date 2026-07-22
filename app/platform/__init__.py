"""Platform infrastructure shared across agent_runtime and product domains.

承载跨层通用基础设施，不含业务规则。
"""
from app.platform.companion_world_repository import (  # noqa: F401
    HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
    SqlCompanionWorldRepository,
    human_level_proactive_allowed,
)
from app.platform.companion_world_memory import (  # noqa: F401
    build_companion_world_memory_sink,
    compact_companion_world_memory_batch,
)
from app.platform.companion_world_turn import (  # noqa: F401
    read_companion_world_context,
    run_companion_world_turn,
)

__all__ = [
    "SqlCompanionWorldRepository",
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON",
    "human_level_proactive_allowed",
    "build_companion_world_memory_sink",
    "compact_companion_world_memory_batch",
    "read_companion_world_context",
    "run_companion_world_turn",
]
