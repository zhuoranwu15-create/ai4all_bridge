"""Fibre MVP 不向模型开放工具。"""
from app.bootstrap.product_registry import FIBRE_APP_ID
from app.tools.registry import ToolPolicy, ToolRegistry

FIBRE_TOOL_REGISTRY = ToolRegistry(())
FIBRE_TOOL_POLICY = ToolPolicy(
    app_id=FIBRE_APP_ID,
    catalog=FIBRE_TOOL_REGISTRY,
    allowed_names=frozenset(),
)

__all__ = ["FIBRE_TOOL_POLICY", "FIBRE_TOOL_REGISTRY"]
