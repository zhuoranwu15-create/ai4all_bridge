"""分层边界门禁（M0-2，D-12）：领域层不得打穿分层直连 Runtime 内部。

用 stdlib `ast` 静态扫描，不引入 import-linter 等新依赖（CLAUDE.md 禁擅自加依赖）。
规则：`app.domains.companion_world.*` 禁止 import `app.db.*` 与 `app.turn_service`
（含相对 import 与 `from app import db` 形式的规避）。当前领域层为 M0 空骨架，
测试应全绿；一旦有人加入越界 import 即 CI 阻塞。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 仓库根：tests/ 的上一级
REPO_ROOT = Path(__file__).resolve().parents[1]

# 受管的领域层根（相对仓库根的包路径）
DOMAIN_LAYER = REPO_ROOT / "app" / "domains" / "companion_world"

# 禁止被领域层直接依赖的 Runtime 内部模块（绝对模块名前缀）
FORBIDDEN_PREFIXES = ("app.db", "app.turn_service")

# M0 脚手架应就位的空骨架包（含各自 __init__.py）
SCAFFOLD_PACKAGES = (
    "app/domains",
    "app/domains/companion_world",
    "app/agent_runtime",
    "app/platform",
)


def _module_package(py_file: Path) -> str:
    """返回该 .py 文件所属的包全名（等价于运行时 __package__）。

    对包内任意模块与 __init__.py，__package__ 都是其所在目录的点分路径。
    """
    rel = py_file.resolve().relative_to(REPO_ROOT)
    return ".".join(rel.parent.parts)


def _resolve_relative(module: str | None, level: int, package: str) -> str | None:
    """把相对 import 解析为绝对模块名（对齐 importlib 的 _resolve_name）。

    level==0 为绝对 import，原样返回 module；越过顶层包无法解析时返回 None
    （交给 Python 自身报错，边界门不误判）。
    """
    if level == 0:
        return module
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None
    base = bits[0]
    return f"{base}.{module}" if module else base


def _iter_imported_modules(tree: ast.AST, package: str):
    """产出一个模块引入的全部绝对模块名（含相对 import 解析）。

    对 `from X import Y` 同时产出 X 与 `X.Y`，覆盖 `from app import db` /
    `from ... import turn_service` 这类以子模块名规避前缀检查的写法。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(node.module, node.level, package)
            if base is None:
                continue
            yield base
            for alias in node.names:
                if alias.name != "*":
                    yield f"{base}.{alias.name}"


def _is_forbidden(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in FORBIDDEN_PREFIXES
    )


def _domain_py_files() -> list[Path]:
    if not DOMAIN_LAYER.exists():
        return []
    return sorted(DOMAIN_LAYER.rglob("*.py"))


def test_scaffold_packages_exist():
    """M0-4：三层空骨架包与 __init__.py 就位（AST 边界门可识别层）。"""
    for pkg in SCAFFOLD_PACKAGES:
        init = REPO_ROOT / pkg / "__init__.py"
        assert init.is_file(), f"缺少脚手架包 __init__.py：{pkg}/__init__.py"


def test_companion_world_does_not_import_runtime_internals():
    """M0-2：领域层不得直连 app.db.* / app.turn_service（D-12，CI 阻塞门）。"""
    violations: list[str] = []
    for py_file in _domain_py_files():
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module):
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel} → import {module}")
    assert not violations, (
        "领域层越界依赖 Runtime 内部（应经 agent_runtime 端口，见 ADR §7.3）：\n"
        + "\n".join(violations)
    )
