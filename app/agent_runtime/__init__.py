"""Agent Runtime facade/port layer（形态无关；account_id = 一段 AI 关系的隔离容器）。

自 R1b（M2-A）起承载 ADR §7.3 四接缝端口契约，定义见 app.agent_runtime.ports。
本层只承载形态无关端口与现有 turn adapter；产品资源解析、共享数据 I/O 和输入组装留在
platform composition，并由 tests/test_layer_boundaries.py 阻止反向依赖产品模块。
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
