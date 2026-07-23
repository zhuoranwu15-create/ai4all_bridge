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

# 受管的 Agent Runtime 层根（形态无关；不得反向依赖任何产品域层）
RUNTIME_LAYER = REPO_ROOT / "app" / "agent_runtime"

# 禁止被领域层直接依赖的 Runtime 内部模块（绝对模块名前缀）
FORBIDDEN_PREFIXES = ("app.db", "app.turn_service")

# 禁止被 Agent Runtime 反向依赖的产品域层（D-06 形态无关：Runtime 不认识 Companion World）
RUNTIME_FORBIDDEN_PREFIXES = ("app.domains", "app.db.companion_world")

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


def _is_forbidden(module: str, prefixes: tuple[str, ...] = FORBIDDEN_PREFIXES) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in prefixes
    )


def _domain_py_files() -> list[Path]:
    if not DOMAIN_LAYER.exists():
        return []
    return sorted(DOMAIN_LAYER.rglob("*.py"))


def _runtime_py_files() -> list[Path]:
    if not RUNTIME_LAYER.exists():
        return []
    return sorted(RUNTIME_LAYER.rglob("*.py"))


def _module_export_names(module_rel: str) -> tuple[str, ...]:
    """AST 静态读取某模块 __all__ 的字符串成员（不 import，沿用本文件纯 AST 纪律）。

    用于把「经 `app/db/__init__.py` 的 `import *` 再导出」的符号名封进门禁：AI 路径写
    `from app.db import insert_human_message` 会 yield `app.db.insert_human_message`，
    绕过「完整模块路径」前缀检查；逐个再导出符号名封住这条缝（见 D-11 门禁）。
    """
    module_file = REPO_ROOT / module_rel
    tree = ast.parse(module_file.read_text(encoding="utf-8"), filename=str(module_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return tuple(
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
    return ()


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


def test_runtime_does_not_import_product_domains():
    """D-06：Agent Runtime 形态无关，禁止反向依赖任何产品域层（app.domains.*）。

    「读+渲染」的 L3 组合属产品域层（`app.domains.companion_world.l3_context`），Runtime 只留
    形态无关的读 I/O。依赖方向须为 `域层 → agent_runtime`，反向即破 D-06——本门禁堵住
    finding ④ 那类「Runtime import 域层渲染函数」的回归（旧一向门禁只扫域层→Runtime、漏此向）。
    """
    violations: list[str] = []
    for py_file in _runtime_py_files():
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module, RUNTIME_FORBIDDEN_PREFIXES):
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel} → import {module}")
    assert not violations, (
        "Agent Runtime 反向依赖产品域层（破 D-06 形态无关；组合应下沉域层）：\n"
        + "\n".join(violations)
    )


def test_runtime_adapter_has_no_companion_world_composition_methods():
    """D-06：产品 conversation/L3/World turn 组装不得回流 Runtime adapter。"""
    from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter

    assert not hasattr(DefaultAgentRuntimeAdapter, "resolve_conversation_account")
    assert not hasattr(DefaultAgentRuntimeAdapter, "send_companion_world_turn")


def test_ai_paths_do_not_import_human_chat_storage():
    """D-11：真人消息不得进入 turn/prompt/dreaming/memory/proactive/Runtime。"""
    roots = [
        REPO_ROOT / "app" / "turn_service.py",
        REPO_ROOT / "app" / "prompt_builder.py",
        REPO_ROOT / "app" / "dreaming.py",
        REPO_ROOT / "app" / "dreaming_scheduler.py",
        REPO_ROOT / "app" / "memory_writer.py",
        REPO_ROOT / "app" / "agent_runtime",
        REPO_ROOT / "app" / "proactive",
    ]
    # `app/db/__init__.py` 以 `from app.db.companion_world_human_chat import *` 再导出全部写
    # helper；仅禁完整模块路径会漏掉 `from app.db import insert_human_message` 这条再导出缝。
    # 逐个把 human-chat 的 __all__ 符号名封为 `app.db.<name>`（不封裸 `app.db`，AI 路径合法用它）。
    human_chat_exports = _module_export_names("app/db/companion_world_human_chat.py")
    assert human_chat_exports, (
        "未能解析 app/db/companion_world_human_chat.py 的 __all__；"
        "D-11 再导出门禁将失效，请检查该模块是否仍声明 __all__。"
    )
    forbidden = (
        "app.db.companion_world_human_chat",
        "app.platform.companion_world_human_chat",
        "app.domains.companion_world.human_chat",
        *(f"app.db.{name}" for name in human_chat_exports),
    )
    violations: list[str] = []
    files: list[Path] = []
    for root in roots:
        files.extend(root.rglob("*.py") if root.is_dir() else [root])
    for py_file in files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        package = _module_package(py_file)
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module, forbidden):
                violations.append(f"{py_file.relative_to(REPO_ROOT)} → import {module}")
    assert not violations, "真人聊天越界进入 AI 路径：\n" + "\n".join(violations)
