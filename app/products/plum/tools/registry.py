"""Plum MVP 不向模型开放工具。"""
from app.bootstrap.product_registry import PLUM_APP_ID
from app.tools.registry import ToolPolicy, ToolRegistry

PLUM_TOOL_REGISTRY = ToolRegistry(())
PLUM_TOOL_POLICY = ToolPolicy(
    app_id=PLUM_APP_ID,
    catalog=PLUM_TOOL_REGISTRY,
    allowed_names=frozenset(),
)

__all__ = ["PLUM_TOOL_POLICY", "PLUM_TOOL_REGISTRY"]
