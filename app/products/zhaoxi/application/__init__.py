"""朝夕应用服务的懒加载 façade。

保留既有集中导入面，同时避免导入任意 application 子模块时加载整棵 Companion World
composition，防止 Runtime adapter 与产品 turn adapter 形成初始化环。
"""
from importlib import import_module


_EXPORTS = {
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "SqlCompanionWorldRepository": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "count_human_proactive_inbound_after": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "get_human_proactive_last_inbound_at": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "human_level_app_route": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "human_level_proactive_allowed": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "resolve_human_proactive_scope": "app.products.zhaoxi.infrastructure.repositories.companion_world",
    "build_companion_world_memory_sink": "app.products.zhaoxi.application.companion_world_memory",
    "compact_companion_world_memory_batch": "app.products.zhaoxi.application.companion_world_memory",
    "read_companion_world_context": "app.products.zhaoxi.application.companion_world_turn",
    "run_companion_world_turn": "app.products.zhaoxi.application.companion_world_turn",
    "CompanionWorldLifecycleService": "app.products.zhaoxi.application.companion_world_lifecycle",
    "LifecycleCommitError": "app.products.zhaoxi.application.companion_world_lifecycle",
    "approve_lifecycle_event": "app.products.zhaoxi.application.companion_world_lifecycle",
    "build_lifecycle_policy": "app.products.zhaoxi.application.companion_world_lifecycle",
    "correct_lifecycle_event": "app.products.zhaoxi.application.companion_world_lifecycle",
    "get_lifecycle_review_event": "app.products.zhaoxi.application.companion_world_lifecycle",
    "list_lifecycle_review_events": "app.products.zhaoxi.application.companion_world_lifecycle",
    "CompanionWorldMailboxService": "app.products.zhaoxi.application.companion_world_mailbox",
    "MailboxError": "app.products.zhaoxi.application.companion_world_mailbox",
    "build_mailbox_policy": "app.products.zhaoxi.application.companion_world_mailbox",
    "create_mailbox_catalog_entry": "app.products.zhaoxi.application.companion_world_mailbox",
    "list_mailbox_catalog": "app.products.zhaoxi.application.companion_world_mailbox",
    "retire_mailbox_catalog_entry": "app.products.zhaoxi.application.companion_world_mailbox",
    "CompanionWorldVisitService": "app.products.zhaoxi.application.companion_world_visits",
    "VisitError": "app.products.zhaoxi.application.companion_world_visits",
    "CompanionWorldHumanChatService": "app.products.zhaoxi.application.companion_world_human_chat",
    "HumanChatError": "app.products.zhaoxi.application.companion_world_human_chat",
    "AppInboxAdapter": "app.products.zhaoxi.infrastructure.app_inbox",
    "AppInboxIntent": "app.products.zhaoxi.infrastructure.app_inbox",
    "HumanAppInboxClaim": "app.products.zhaoxi.infrastructure.app_inbox",
    "HumanAppInboxIntent": "app.products.zhaoxi.infrastructure.app_inbox",
    "SqlAppNotificationRepository": "app.products.zhaoxi.infrastructure.app_inbox",
}


def __getattr__(name: str):
    """按需解析兼容导出，避免 package import 触发无关应用服务初始化。"""

    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value

__all__ = [
    "SqlCompanionWorldRepository",
    "SqlAppNotificationRepository",
    "AppInboxAdapter",
    "AppInboxIntent",
    "CompanionWorldLifecycleService",
    "CompanionWorldMailboxService",
    "CompanionWorldVisitService",
    "CompanionWorldHumanChatService",
    "LifecycleCommitError",
    "MailboxError",
    "VisitError",
    "HumanChatError",
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
