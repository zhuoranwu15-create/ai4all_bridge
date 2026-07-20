"""Agent Runtime facade/port layer（形态无关；account_id = 一段 AI 关系的隔离容器）。

自 R1b（M2-A）起承载 ADR §7.3 四接缝端口契约，定义见 app.agent_runtime.ports。
本层允许直接依赖 app.db.* / app.turn_service（是域层→Runtime 的受管接缝），不受
tests/test_layer_boundaries.py 门禁约束（门禁仅扫 app.domains.companion_world.*）。
"""
from app.agent_runtime.ports import (  # noqa: F401
    AgentRuntimePort,
    DeliveryResult,
    MemoryEvent,
    MemoryProvenance,
    MemorySink,
    ProactiveDeliveryAdapter,
    ProactiveIntent,
    ResidentHandle,
    UnitOfWork,
)

__all__ = [
    "AgentRuntimePort",
    "DeliveryResult",
    "MemoryEvent",
    "MemoryProvenance",
    "MemorySink",
    "ProactiveDeliveryAdapter",
    "ProactiveIntent",
    "ResidentHandle",
    "UnitOfWork",
]
