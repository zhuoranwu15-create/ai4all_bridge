"""Companion World product domain layer（朝夕相伴 多居民私人世界）。

分层不变量：本包只经 agent_runtime 端口访问 Agent Runtime，禁止直接
import app.db.* / app.turn_service（由 tests/test_layer_boundaries.py 的
stdlib-AST 边界门禁执行，D-12）。
"""
from app.domains.companion_world.contracts import (  # noqa: F401
    BootstrapResult,
    AppNotificationRecord,
    AppNotificationRepository,
    CandidateRecord,
    CompanionWorldError,
    ConversationMessage,
    ConversationSummary,
    ConversationTarget,
    FeedRepository,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
    TemplateRecord,
    UniversePostRecord,
    WorldRecord,
    WorldOutboxRecord,
    WorldRepository,
)
from app.domains.companion_world.service import CompanionWorldService  # noqa: F401
from app.domains.companion_world.feed import (  # noqa: F401
    CompanionWorldFeedService,
    user_post_fingerprint,
)
from app.domains.companion_world.notifications import (  # noqa: F401
    CompanionWorldNotificationService,
)
from app.domains.companion_world.proactive import (  # noqa: F401
    HUMAN_PROACTIVE_CATEGORIES,
    HumanProactiveDecision,
    HumanProactiveScope,
    HumanProactiveSpeaker,
    allows_speaker_reselection,
    decide_human_proactive_delivery,
    is_human_proactive_category,
)
from app.domains.companion_world.memory_sink import (  # noqa: F401
    CompanionWorldMemorySink,
)

__all__ = [
    "BootstrapResult",
    "AppNotificationRecord",
    "AppNotificationRepository",
    "CandidateRecord",
    "CompanionWorldError",
    "CompanionWorldMemorySink",
    "CompanionWorldNotificationService",
    "CompanionWorldFeedService",
    "CompanionWorldService",
    "ConversationMessage",
    "ConversationSummary",
    "ConversationTarget",
    "FeedRepository",
    "HUMAN_PROACTIVE_CATEGORIES",
    "HumanProactiveDecision",
    "HumanProactiveScope",
    "HumanProactiveSpeaker",
    "ResidentRecord",
    "ResidentSelection",
    "TemplateDraft",
    "TemplateRecord",
    "UniversePostRecord",
    "WorldRecord",
    "WorldOutboxRecord",
    "WorldRepository",
    "allows_speaker_reselection",
    "decide_human_proactive_delivery",
    "is_human_proactive_category",
    "user_post_fingerprint",
]
