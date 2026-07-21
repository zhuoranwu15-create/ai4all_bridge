"""Companion World product domain layer（朝夕相伴 多居民私人世界）。

分层不变量：本包只经 agent_runtime 端口访问 Agent Runtime，禁止直接
import app.db.* / app.turn_service（由 tests/test_layer_boundaries.py 的
stdlib-AST 边界门禁执行，D-12）。
"""
from app.domains.companion_world.contracts import (  # noqa: F401
    BootstrapResult,
    CandidateRecord,
    CompanionWorldError,
    ConversationMessage,
    ConversationSummary,
    ConversationTarget,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
    TemplateRecord,
    WorldRecord,
    WorldRepository,
)
from app.domains.companion_world.service import CompanionWorldService  # noqa: F401

__all__ = [
    "BootstrapResult",
    "CandidateRecord",
    "CompanionWorldError",
    "CompanionWorldService",
    "ConversationMessage",
    "ConversationSummary",
    "ConversationTarget",
    "ResidentRecord",
    "ResidentSelection",
    "TemplateDraft",
    "TemplateRecord",
    "WorldRecord",
    "WorldRepository",
]
