"""Platform infrastructure shared across agent_runtime and product domains.

承载跨层通用基础设施，不含业务规则。
"""
from app.platform.companion_world_repository import (  # noqa: F401
    HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
    SqlCompanionWorldRepository,
    count_human_proactive_inbound_after,
    get_human_proactive_last_inbound_at,
    human_level_app_route,
    human_level_proactive_allowed,
    resolve_human_proactive_scope,
)
from app.platform.companion_world_memory import (  # noqa: F401
    build_companion_world_memory_sink,
    compact_companion_world_memory_batch,
)
from app.platform.companion_world_turn import (  # noqa: F401
    read_companion_world_context,
    run_companion_world_turn,
)
from app.platform.companion_world_lifecycle import (  # noqa: F401
    CompanionWorldLifecycleService,
    build_lifecycle_policy,
    get_lifecycle_review_event,
    list_lifecycle_review_events,
)
from app.platform.app_inbox import (  # noqa: F401
    AppInboxAdapter,
    AppInboxIntent,
    HumanAppInboxClaim,
    HumanAppInboxIntent,
    SqlAppNotificationRepository,
)

__all__ = [
    "SqlCompanionWorldRepository",
    "SqlAppNotificationRepository",
    "AppInboxAdapter",
    "AppInboxIntent",
    "CompanionWorldLifecycleService",
    "HumanAppInboxClaim",
    "HumanAppInboxIntent",
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON",
    "count_human_proactive_inbound_after",
    "get_human_proactive_last_inbound_at",
    "human_level_app_route",
    "human_level_proactive_allowed",
    "resolve_human_proactive_scope",
    "build_companion_world_memory_sink",
    "build_lifecycle_policy",
    "compact_companion_world_memory_batch",
    "get_lifecycle_review_event",
    "list_lifecycle_review_events",
    "read_companion_world_context",
    "run_companion_world_turn",
]
