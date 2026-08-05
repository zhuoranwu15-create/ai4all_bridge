"""鸣蝉产品应用服务的稳定导出面。"""

from importlib import import_module


_EXPORTS = {
    "HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON": "app.products.mingchan.infrastructure.world_repository",
    "SqlCompanionWorldRepository": "app.products.mingchan.infrastructure.world_repository",
    "count_human_proactive_inbound_after": "app.products.mingchan.infrastructure.world_repository",
    "get_human_proactive_last_inbound_at": "app.products.mingchan.infrastructure.world_repository",
    "human_level_app_route": "app.products.mingchan.infrastructure.world_repository",
    "human_level_proactive_allowed": "app.products.mingchan.infrastructure.world_repository",
    "resolve_human_proactive_scope": "app.products.mingchan.infrastructure.world_repository",
    "build_mingchan_world_memory_sink": "app.products.mingchan.application.memory",
    "compact_mingchan_world_memory_batch": "app.products.mingchan.application.memory",
    "read_companion_world_context": "app.products.mingchan.application.companion_world_turn",
    "run_mingchan_companion_world_turn": "app.products.mingchan.application.companion_world_turn",
    "CompanionWorldLifecycleService": "app.products.mingchan.application.lifecycle",
    "LifecycleCommitError": "app.products.mingchan.application.lifecycle",
    "approve_lifecycle_event": "app.products.mingchan.application.lifecycle",
    "build_lifecycle_policy": "app.products.mingchan.application.lifecycle",
    "correct_lifecycle_event": "app.products.mingchan.application.lifecycle",
    "get_lifecycle_review_event": "app.products.mingchan.application.lifecycle",
    "list_lifecycle_review_events": "app.products.mingchan.application.lifecycle",
    "CompanionWorldMailboxService": "app.products.mingchan.application.mailbox",
    "MailboxError": "app.products.mingchan.application.mailbox",
    "build_mailbox_policy": "app.products.mingchan.application.mailbox",
    "create_mailbox_catalog_entry": "app.products.mingchan.application.mailbox",
    "list_mailbox_catalog": "app.products.mingchan.application.mailbox",
    "retire_mailbox_catalog_entry": "app.products.mingchan.application.mailbox",
    "CompanionWorldVisitService": "app.products.mingchan.application.visits",
    "VisitError": "app.products.mingchan.application.visits",
    "CompanionWorldHumanChatService": "app.products.mingchan.application.human_chat",
    "HumanChatError": "app.products.mingchan.application.human_chat",
    "AppInboxAdapter": "app.products.mingchan.infrastructure.app_inbox",
    "AppInboxIntent": "app.products.mingchan.infrastructure.app_inbox",
    "HumanAppInboxClaim": "app.products.mingchan.infrastructure.app_inbox",
    "HumanAppInboxIntent": "app.products.mingchan.infrastructure.app_inbox",
    "SqlAppNotificationRepository": "app.products.mingchan.infrastructure.app_inbox",
}


def __getattr__(name: str):
    """按需解析鸣蝉应用服务，避免包导入触发整棵 World composition。"""

    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)
