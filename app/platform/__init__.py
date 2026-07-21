"""Platform infrastructure shared across agent_runtime and product domains.

承载跨层通用基础设施，不含业务规则。
"""
from app.platform.companion_world_repository import (  # noqa: F401
    SqlCompanionWorldRepository,
)

__all__ = ["SqlCompanionWorldRepository"]
