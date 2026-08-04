"""鸣蝉产品工具策略；当前只开放形态无关共享工具。"""
from __future__ import annotations

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.tools.registry import SHARED_TOOL_REGISTRY, ToolPolicy


MINGCHAN_TOOL_POLICY = ToolPolicy.allow_all(
    app_id=MINGCHAN_APP_ID,
    catalog=SHARED_TOOL_REGISTRY,
)


__all__ = ["MINGCHAN_TOOL_POLICY"]
