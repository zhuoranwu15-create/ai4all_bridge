"""跨产品共享工具框架与 schema。"""

from app.tools.definitions import (
    get_read_tools,
    get_tdai_search_tools,
    get_web_fetch_tools,
    get_web_search_tools,
)
from app.tools.registry import (
    SHARED_TOOL_REGISTRY,
    ToolBinding,
    ToolPolicy,
    ToolRegistry,
    ToolSpec,
    get_default_tools,
    get_spec,
    iter_specs,
)

__all__ = [
    "SHARED_TOOL_REGISTRY",
    "ToolBinding",
    "ToolPolicy",
    "ToolRegistry",
    "ToolSpec",
    "get_default_tools",
    "get_read_tools",
    "get_spec",
    "get_tdai_search_tools",
    "get_web_fetch_tools",
    "get_web_search_tools",
    "iter_specs",
]
