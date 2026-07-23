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
    LifecycleCommitError,
    approve_lifecycle_event,
    build_lifecycle_policy,
    correct_lifecycle_event,
    get_lifecycle_review_event,
    list_lifecycle_review_events,
)
from app.platform.companion_world_mailbox import (  # noqa: F401
    CompanionWorldMailboxService,
    MailboxError,
    build_mailbox_policy,
    create_mailbox_catalog_entry,
    list_mailbox_catalog,
    retire_mailbox_catalog_entry,
)
from app.platform.companion_world_visits import (  # noqa: F401
    CompanionWorldVisitService,
    VisitError,
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
    "CompanionWorldMailboxService",
    "CompanionWorldVisitService",
    "LifecycleCommitError",
    "MailboxError",
    "VisitError",
    "HumanAppInboxClaim",
    "HumanAppInboxIntent",
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON",
    "count_human_proactive_inbound_after",
    "get_human_proactive_last_inbound_at",
    "human_level_app_route",
    "human_level_proactive_allowed",
    "resolve_human_proactive_scope",
    "build_companion_world_memory_sink",
    "approve_lifecycle_event",
    "build_lifecycle_policy",
    "build_mailbox_policy",
    "compact_companion_world_memory_batch",
    "correct_lifecycle_event",
    "create_mailbox_catalog_entry",
    "get_lifecycle_review_event",
    "list_lifecycle_review_events",
    "list_mailbox_catalog",
    "read_companion_world_context",
    "retire_mailbox_catalog_entry",
    "run_companion_world_turn",
]
