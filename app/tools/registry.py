"""跨产品工具注册框架与共享工具 catalog。"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from app.tools.definitions import (
    get_read_tools,
    get_tdai_search_tools,
    get_web_fetch_tools,
    get_web_search_tools,
)

CALL_PLAIN = "plain"
CALL_INVOCATION = "invocation"
CALL_WEB_SEARCH = "web_search"

DEFAULT_NEVER = "never"


@dataclass(frozen=True)
class ToolBinding:
    """工具 handler 与运行策略元数据。"""

    handler_module: str
    handler_attr: str
    call_style: str = CALL_PLAIN
    runtime_requires_flag: Optional[str] = None
    default_when_flag: Optional[str] = None
    capability_requires: Optional[str] = None
    enabled_reason: str = "default"
    disabled_reason: str = "disabled"


@dataclass(frozen=True)
class ToolSpec:
    """一个模型 schema 与真实 handler 的不可变绑定。"""

    name: str
    schema: dict
    group: str
    handler_module: str
    handler_attr: str
    call_style: str = CALL_PLAIN
    runtime_requires_flag: Optional[str] = None
    default_when_flag: Optional[str] = None
    capability_requires: Optional[str] = None
    enabled_reason: str = "default"
    disabled_reason: str = "disabled"


class ToolRegistry:
    """按稳定顺序保存工具规格，并拒绝重名。"""

    def __init__(self, specs: Iterable[ToolSpec]):
        ordered = tuple(specs)
        by_name: Dict[str, ToolSpec] = {}
        for spec in ordered:
            if spec.name in by_name:
                raise RuntimeError(f"重复的工具名: {spec.name}")
            by_name[spec.name] = spec
        self._specs = ordered
        self._by_name: Mapping[str, ToolSpec] = MappingProxyType(by_name)

    def iter_specs(self) -> List[ToolSpec]:
        """返回注册顺序稳定的规格副本。"""

        return list(self._specs)

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        """按名字返回规格；未知工具返回 None。"""

        return self._by_name.get(name)

    def names(self) -> frozenset[str]:
        """返回 catalog 中全部工具名。"""

        return frozenset(self._by_name)


@dataclass(frozen=True)
class ToolPolicy:
    """一个产品可见、可执行的工具集合及默认选择规则。"""

    app_id: str
    catalog: ToolRegistry
    allowed_names: frozenset[str]
    first_round_choice_resolver: Optional[Callable[[str], Any]] = None

    def __post_init__(self) -> None:
        unknown = self.allowed_names - self.catalog.names()
        if unknown:
            raise ValueError(f"ToolPolicy 包含未知工具: {sorted(unknown)}")

    @classmethod
    def allow_all(
        cls,
        *,
        app_id: str,
        catalog: ToolRegistry,
        first_round_choice_resolver: Optional[Callable[[str], Any]] = None,
    ) -> "ToolPolicy":
        """构造允许 catalog 全部工具的产品策略。"""

        return cls(
            app_id=app_id,
            catalog=catalog,
            allowed_names=catalog.names(),
            first_round_choice_resolver=first_round_choice_resolver,
        )

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        """返回当前产品允许的规格；已知但未允许也返回 None。"""

        if name not in self.allowed_names:
            return None
        return self.catalog.get_spec(name)

    def iter_specs(self) -> List[ToolSpec]:
        """按 catalog 顺序返回当前产品允许的全部规格。"""

        return [
            spec for spec in self.catalog.iter_specs() if spec.name in self.allowed_names
        ]

    def get_default_tools(
        self,
        *,
        flags: Optional[Mapping[str, bool]] = None,
        capabilities: Optional[Mapping[str, bool]] = None,
    ) -> list:
        """按产品 flag 与渠道能力返回默认模型可见 schema。"""

        resolved_flags = flags or {}
        resolved_capabilities = capabilities or {}
        out = []
        for spec in self.iter_specs():
            flag = spec.default_when_flag
            if flag == DEFAULT_NEVER:
                include = False
            elif flag is None:
                include = True
            else:
                include = bool(resolved_flags.get(flag))
            if spec.capability_requires:
                include = include and bool(
                    resolved_capabilities.get(spec.capability_requires)
                )
            if include:
                out.append(spec.schema)
        return out

    def first_round_tool_choice(self, text: str) -> Any:
        """返回产品首轮 tool choice；未配置策略时为 auto。"""

        if self.first_round_choice_resolver is None:
            return "auto"
        return self.first_round_choice_resolver(text)


def build_specs(
    *,
    group_providers: Sequence[tuple[str, Callable[[], list]]],
    bindings: Mapping[str, ToolBinding],
) -> List[ToolSpec]:
    """从 schema provider 与 handler bindings 构造并双向校验规格。"""

    specs: List[ToolSpec] = []
    seen: set[str] = set()
    for group, provider in group_providers:
        for schema in provider():
            name = schema["function"]["name"]
            if name in seen:
                raise RuntimeError(f"重复的工具名: {name}")
            seen.add(name)
            binding = bindings.get(name)
            if binding is None:
                raise RuntimeError(f"工具 schema '{name}' 缺少 handler binding")
            specs.append(
                ToolSpec(
                    name=name,
                    schema=schema,
                    group=group,
                    handler_module=binding.handler_module,
                    handler_attr=binding.handler_attr,
                    call_style=binding.call_style,
                    runtime_requires_flag=binding.runtime_requires_flag,
                    default_when_flag=binding.default_when_flag,
                    capability_requires=binding.capability_requires,
                    enabled_reason=binding.enabled_reason,
                    disabled_reason=binding.disabled_reason,
                )
            )
    missing = set(bindings) - seen
    if missing:
        raise RuntimeError(f"handler binding 没有对应 schema: {sorted(missing)}")
    return specs


SHARED_TOOL_BINDINGS: Mapping[str, ToolBinding] = MappingProxyType(
    {
        "web_fetch": ToolBinding("app.tools.web_fetch_handlers", "handle_web_fetch"),
        "read": ToolBinding("app.tools.read_handlers", "handle_read"),
        "web_search": ToolBinding(
            "app.tools.web_search_handlers",
            "handle_web_search",
            call_style=CALL_WEB_SEARCH,
            runtime_requires_flag="web_search_enabled",
            default_when_flag="web_search_enabled",
            enabled_reason="web_search_enabled",
            disabled_reason="web_search_disabled",
        ),
        "tdai_memory_search": ToolBinding(
            "app.tools.tdai_search_handlers",
            "handle_tdai_memory_search",
            runtime_requires_flag="tdai_search_enabled",
            default_when_flag="tdai_search_enabled",
            enabled_reason="tdai_search_enabled",
            disabled_reason="tdai_search_disabled",
        ),
        "tdai_conversation_search": ToolBinding(
            "app.tools.tdai_search_handlers",
            "handle_tdai_conversation_search",
            runtime_requires_flag="tdai_search_enabled",
            default_when_flag="tdai_search_enabled",
            enabled_reason="tdai_search_enabled",
            disabled_reason="tdai_search_disabled",
        ),
    }
)

SHARED_GROUP_PROVIDERS = (
    ("web_fetch", get_web_fetch_tools),
    ("read", get_read_tools),
    ("web_search", get_web_search_tools),
    ("tdai_search", get_tdai_search_tools),
)
SHARED_TOOL_REGISTRY = ToolRegistry(
    build_specs(
        group_providers=SHARED_GROUP_PROVIDERS,
        bindings=SHARED_TOOL_BINDINGS,
    )
)


def iter_specs() -> List[ToolSpec]:
    """兼容返回共享工具规格；产品工具从各产品 registry 获取。"""

    return SHARED_TOOL_REGISTRY.iter_specs()


def get_spec(name: str) -> Optional[ToolSpec]:
    """兼容查询共享工具规格。"""

    return SHARED_TOOL_REGISTRY.get_spec(name)


def get_default_tools(
    *, web_search_enabled: bool = False, tdai_search_enabled: bool = False
) -> list:
    """返回共享工具默认集合。"""

    policy = ToolPolicy.allow_all(app_id="shared", catalog=SHARED_TOOL_REGISTRY)
    return policy.get_default_tools(
        flags={
            "web_search_enabled": web_search_enabled,
            "tdai_search_enabled": tdai_search_enabled,
        }
    )


__all__ = [
    "CALL_INVOCATION",
    "CALL_PLAIN",
    "CALL_WEB_SEARCH",
    "DEFAULT_NEVER",
    "SHARED_GROUP_PROVIDERS",
    "SHARED_TOOL_BINDINGS",
    "SHARED_TOOL_REGISTRY",
    "ToolBinding",
    "ToolPolicy",
    "ToolRegistry",
    "ToolSpec",
    "build_specs",
    "get_default_tools",
    "get_spec",
    "iter_specs",
]
