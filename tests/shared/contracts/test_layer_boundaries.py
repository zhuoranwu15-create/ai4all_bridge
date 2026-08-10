"""通用分层边界门禁：产品、Runtime 与平台依赖方向由 AST 强制。"""
from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

# 仓库根：tests/shared/contracts/ 的上三级
REPO_ROOT = Path(__file__).resolve().parents[3]

# 产品根；每个产品在自己的 ``domain`` 子包中保持纯领域层。
PRODUCTS_ROOT = REPO_ROOT / "app" / "products"

# 受管的 Agent Runtime 层根（形态无关；不得反向依赖任何产品域层）
RUNTIME_LAYER = REPO_ROOT / "app" / "agent_runtime"

# 共享平台层不得依赖任何产品实现；产品与平台由 bootstrap composition root 接线。
PLATFORM_LAYER = REPO_ROOT / "app" / "platform"

# 共享工具框架不得反向组合产品 schema/handler；产品 catalog 由产品自身持有。
SHARED_TOOLS_LAYER = REPO_ROOT / "app" / "tools"

# 已实体化的朝夕垂直业务不得重新漂回 app/ 顶层。
ZHAOXI_PROACTIVE_LAYER = PRODUCTS_ROOT / "zhaoxi" / "proactive"

# 禁止被领域层直接依赖的 Runtime 内部模块（绝对模块名前缀）
DOMAIN_FORBIDDEN_PREFIXES = (
    "app.db",
    "app.platform",
    "app.routers",
    "app.turn_service",
    "fastapi",
    "psycopg",
    "sqlalchemy",
    "sqlite3",
    "starlette",
)

# 禁止被 Agent Runtime 反向依赖的产品域层（D-06 形态无关：Runtime 不认识 Companion World）
RUNTIME_FORBIDDEN_PREFIXES = (
    "app.products",
)

PLATFORM_FORBIDDEN_PREFIXES = (
    "app.products",
)

# M0 脚手架应就位的空骨架包（含各自 __init__.py）
SCAFFOLD_PACKAGES = (
    "app/agent_runtime",
    "app/agent_runtime/turns",
    "app/bootstrap",
    "app/platform",
    "app/products",
    "app/products/mingchan",
    "app/products/mingchan/api",
    "app/products/mingchan/application",
    "app/products/mingchan/domain",
    "app/products/mingchan/infrastructure",
    "app/products/mingchan/jobs",
    "app/products/mingchan/tools",
    "app/products/zhaoxi",
    "app/products/zhaoxi/domain",
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


def _is_forbidden(
    module: str, prefixes: tuple[str, ...] = DOMAIN_FORBIDDEN_PREFIXES
) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in prefixes
    )


def _product_roots() -> list[tuple[str, Path]]:
    """返回全部产品包，供跨产品依赖门禁扫描。"""

    if not PRODUCTS_ROOT.exists():
        return []
    return [
        (f"app.products.{child.name}", child)
        for child in sorted(PRODUCTS_ROOT.iterdir())
        if child.is_dir()
        and child.name != "__pycache__"
        and (child / "__init__.py").is_file()
    ]


def _product_domain_roots() -> list[tuple[str, Path]]:
    """返回各产品明确声明的纯 domain 子包。"""

    return [
        (f"{module}.domain", root / "domain")
        for module, root in _product_roots()
        if (root / "domain" / "__init__.py").is_file()
    ]


def _domain_py_files() -> list[Path]:
    return sorted(
        py_file
        for _module, root in _product_domain_roots()
        for py_file in root.rglob("*.py")
    )


def _runtime_py_files() -> list[Path]:
    if not RUNTIME_LAYER.exists():
        return []
    return sorted(RUNTIME_LAYER.rglob("*.py"))


def _platform_py_files() -> list[Path]:
    if not PLATFORM_LAYER.exists():
        return []
    return sorted(PLATFORM_LAYER.rglob("*.py"))


def _node_mentions_product_discriminator(node: ast.AST) -> bool:
    """判断表达式是否引用 app_id/product_id，供禁止 Runtime 产品字符串分支。"""
    return any(
        (isinstance(child, ast.Name) and child.id in {"app_id", "product_id"})
        or (
            isinstance(child, ast.Attribute)
            and child.attr in {"app_id", "product_id"}
        )
        for child in ast.walk(node)
    )


def _module_export_names(module_rel: str) -> tuple[str, ...]:
    """AST 静态读取某模块 __all__ 的字符串成员（不 import，沿用本文件纯 AST 纪律）。

    用于把「经 `app/db/__init__.py` 懒加载再导出」的符号名封进门禁：AI 路径写
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


def test_product_business_packages_are_not_top_level():
    """朝夕主动消息已归位；旧顶层包不得以 Python 源码形式复活。"""

    assert (ZHAOXI_PROACTIVE_LAYER / "__init__.py").is_file()
    assert not list((REPO_ROOT / "app" / "proactive").rglob("*.py"))
    assert not list((REPO_ROOT / "app" / "moderation").rglob("*.py"))
    assert (PLATFORM_LAYER / "moderation" / "__init__.py").is_file()
    for old_file in (
        "app/db/mission.py",
        "app/db/notifications.py",
        "app/db/proactive.py",
        "app/db/user_meta.py",
        "app/db/campaign.py",
        "app/db/campaign_analytics.py",
        "app/routers/admin_dreaming.py",
        "app/routers/admin_accounts.py",
        "app/routers/admin_campaigns.py",
        "app/routers/admin_moderation.py",
        "app/routers/admin_proactive.py",
        "app/routers/admin_security.py",
        "app/routers/bridge.py",
        "app/routers/debug.py",
        "app/routers/app_api.py",
        "app/tools/commitment_handlers.py",
        "app/tools/content_invitation_handlers.py",
        "app/tools/mission_handlers.py",
        "app/tools/proactive_settings_handlers.py",
        "app/tools/reminder_handlers.py",
        "app/tools/session_status_handlers.py",
    ):
        assert not (REPO_ROOT / old_file).exists(), f"产品实现重新漂回旧路径：{old_file}"


def test_product_domains_do_not_import_framework_or_runtime_internals():
    """产品 domain 不得直连 FastAPI、SQL repository、router 或 turn_service。"""
    violations: list[str] = []
    for py_file in _domain_py_files():
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module, DOMAIN_FORBIDDEN_PREFIXES):
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel} → import {module}")
    assert not violations, (
        "产品 domain 越界依赖框架/SQL/Runtime 内部（应经端口或 application service）：\n"
        + "\n".join(violations)
    )


def test_product_domains_do_not_import_each_other():
    """任意产品域不得 import 另一产品；composition 只允许发生在 bootstrap。"""
    product_roots = _product_roots()
    violations: list[str] = []
    for current_module, root in product_roots:
        forbidden = tuple(
            module for module, _other_root in product_roots if module != current_module
        )
        for py_file in sorted(root.rglob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            package = _module_package(py_file)
            for module in _iter_imported_modules(tree, package):
                if forbidden and _is_forbidden(module, forbidden):
                    violations.append(
                        f"{py_file.relative_to(REPO_ROOT)} → import {module}"
                    )
    assert not violations, "产品域互相依赖（应只在 bootstrap 组合）：\n" + "\n".join(
        violations
    )


def test_companion_world_persistence_is_owned_by_mingchan_only():
    """World SQL 原语只能保留鸣蝉 owner，禁止在朝夕目录重新形成副本。"""

    module_names = (
        "companion_world.py",
        "companion_world_human_chat.py",
        "companion_world_lifecycle.py",
        "companion_world_mailbox.py",
        "companion_world_visits.py",
        "notifications.py",
        "resident_wishes.py",
    )
    mingchan_root = (
        REPO_ROOT / "app" / "products" / "mingchan" / "infrastructure" / "persistence"
    )
    zhaoxi_root = (
        REPO_ROOT / "app" / "products" / "zhaoxi" / "infrastructure" / "persistence"
    )
    for module_name in module_names:
        assert (mingchan_root / module_name).is_file(), f"鸣蝉缺少 {module_name}"
        assert not (zhaoxi_root / module_name).exists(), f"朝夕重新出现 {module_name}"


def test_runtime_does_not_import_product_domains():
    """D-06：Agent Runtime 形态无关，禁止反向依赖任何产品实现。

    「读+渲染」的 L3 组合属产品域层（`app.products.mingchan.domain.companion_world.l3_context`），Runtime 只留
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


def test_shared_platform_does_not_import_product_domains():
    """共享 platform 不得认识产品。"""
    violations: list[str] = []
    for py_file in _platform_py_files():
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module, PLATFORM_FORBIDDEN_PREFIXES):
                violations.append(f"{py_file.relative_to(REPO_ROOT)} → import {module}")
    assert not violations, (
        "共享 Platform 依赖具体产品（产品实现只能由 bootstrap 组合）：\n"
        + "\n".join(violations)
    )


def test_shared_tools_do_not_import_product_implementations():
    """共享 ToolRegistry/executor 只能认识共享工具，产品 catalog 在产品边界内组合。"""

    violations: list[str] = []
    for py_file in sorted(SHARED_TOOLS_LAYER.rglob("*.py")):
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for module in _iter_imported_modules(tree, package):
            if _is_forbidden(module, ("app.products",)):
                violations.append(f"{py_file.relative_to(REPO_ROOT)} → import {module}")
    assert not violations, "共享 tools 反向依赖产品实现：\n" + "\n".join(violations)


def test_main_is_only_an_asgi_bootstrap_entrypoint():
    """main 不得重新组合产品 router、scheduler、DB 或 turn 业务。"""

    main_file = REPO_ROOT / "app" / "main.py"
    tree = ast.parse(main_file.read_text(encoding="utf-8"), filename=str(main_file))
    imported_app_modules = {
        module
        for module in _iter_imported_modules(tree, "app")
        if module.startswith("app.")
    }
    assert imported_app_modules <= {
        "app.bootstrap.application",
        "app.bootstrap.application.create_app",
        "app.config",
        "app.config.settings",
    }
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for node in tree.body)


def test_transition_facades_have_real_owners_and_no_business_definitions():
    """根过渡模块只做兼容导出，真实实现必须留在 Runtime/朝夕 owner。"""

    expected = {
        "app/turn_service.py": "app/agent_runtime/turns/service.py",
        "app/prompt_builder.py": "app/agent_runtime/context/prompt_builder.py",
        "app/reminder_utils.py": (
            "app/products/zhaoxi/proactive/obligations/reminder_schedule.py"
        ),
    }
    for facade_rel, owner_rel in expected.items():
        facade = REPO_ROOT / facade_rel
        owner = REPO_ROOT / owner_rel
        assert facade.is_file() and owner.is_file()
        tree = ast.parse(facade.read_text(encoding="utf-8"), filename=str(facade))
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in tree.body
        ), f"兼容 façade 重新出现业务定义：{facade_rel}"


def test_shared_platform_does_not_import_db_compatibility_facade():
    """Platform 只能依赖明确 persistence 模块，不能经 app.db 隐式加载产品。"""

    violations: list[str] = []
    for py_file in _platform_py_files():
        package = _module_package(py_file)
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        if "app.db" in _iter_imported_modules(tree, package):
            violations.append(str(py_file.relative_to(REPO_ROOT)))
    assert not violations, (
        "共享 Platform 依赖 app.db 兼容 façade（请改为明确 persistence 模块）：\n"
        + "\n".join(violations)
    )


def test_runtime_has_no_product_string_branch():
    """Runtime 不得按 app_id/product_id 字符串分支，产品差异必须经端口注入。"""
    violations: list[str] = []
    for py_file in _runtime_py_files():
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not _node_mentions_product_discriminator(node):
                continue
            # 下标读取 ``access[\"app_id\"]`` 自身也包含字符串常量；这里只禁
            # ``app_id == \"zhaoxi\"`` 这类产品字面量分支，不禁两个动态 scope 做相等校验。
            if any(
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value not in {"app_id", "product_id"}
                for child in ast.walk(node)
            ):
                violations.append(
                    f"{py_file.relative_to(REPO_ROOT)}:{node.lineno} → product string compare"
                )
    assert not violations, "Runtime 出现产品字符串分支：\n" + "\n".join(violations)


def test_runtime_adapter_has_no_companion_world_composition_methods():
    """D-06：产品 conversation/L3/World turn 组装不得回流 Runtime adapter。"""
    from app.agent_runtime.adapter import DefaultAgentRuntimeAdapter

    assert not hasattr(DefaultAgentRuntimeAdapter, "resolve_conversation_account")
    assert not hasattr(DefaultAgentRuntimeAdapter, "send_companion_world_turn")


def test_zhaoxi_application_package_does_not_export_companion_world():
    """朝夕 application 不再承担鸣蝉 World 的兼容 façade。"""

    from app.products.zhaoxi import application

    assert not hasattr(application, "_EXPORTS")
    assert not hasattr(application, "build_companion_world_memory_sink")
    assert not hasattr(application, "compact_companion_world_memory_batch")


def test_db_compatibility_facade_resolves_owned_persistence_exports():
    """移动后的 persistence 公共 API 仍可经 app.db 兼容入口解析。"""

    import app.db as db

    for module_name in db._COMPAT_EXPORT_MODULES:
        module = import_module(module_name)
        exports = getattr(module, "__all__", ())
        assert exports, f"兼容 persistence 未声明 __all__：{module_name}"
        for name in exports:
            assert getattr(db, name) is getattr(module, name)


def test_zhaoxi_db_facade_imports_resolve_during_product_migration():
    """迁移期间朝夕旧 World 调用的 ``app.db`` 符号必须都可解析。"""

    import app.db as db

    zhaoxi_root = REPO_ROOT / "app" / "products" / "zhaoxi"
    unresolved: list[str] = []
    for py_file in sorted(zhaoxi_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        aliases: set[str] = set()
        direct_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "app.db"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "app.db":
                direct_names.update(alias.name for alias in node.names)
        used_names = set(direct_names)
        used_names.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        )
        for name in sorted(used_names):
            if not hasattr(db, name):
                unresolved.append(
                    f"{py_file.relative_to(REPO_ROOT)} → app.db.{name}"
                )
    assert not unresolved, "app.db 过渡 façade 缺少导出：\n" + "\n".join(unresolved)


def test_ai_paths_do_not_import_human_chat_storage():
    """D-11：真人消息不得进入 turn/prompt/dreaming/memory/proactive/Runtime。"""
    roots = [
        REPO_ROOT / "app" / "turn_service.py",
        REPO_ROOT / "app" / "prompt_builder.py",
        REPO_ROOT / "app" / "products" / "zhaoxi" / "application" / "memory",
        REPO_ROOT / "app" / "products" / "zhaoxi" / "jobs" / "dreaming",
        REPO_ROOT / "app" / "agent_runtime",
        ZHAOXI_PROACTIVE_LAYER,
    ]
    # `app/db/__init__.py` 会按 __all__ 懒加载再导出全部 human-chat 写 helper；仅禁完整
    # 模块路径会漏掉 `from app.db import insert_human_message` 这条兼容 façade 缝。
    # 逐个把 human-chat 的 __all__ 符号名封为 `app.db.<name>`（不封裸 `app.db`，AI 路径合法用它）。
    human_chat_module = (
        "app/products/mingchan/infrastructure/persistence/"
        "companion_world_human_chat.py"
    )
    human_chat_exports = _module_export_names(human_chat_module)
    assert human_chat_exports, (
        f"未能解析 {human_chat_module} 的 __all__；"
        "D-11 再导出门禁将失效，请检查该模块是否仍声明 __all__。"
    )
    forbidden = (
        "app.products.mingchan.infrastructure.persistence.companion_world_human_chat",
        "app.products.mingchan.application.human_chat",
        "app.products.mingchan.domain.companion_world.human_chat",
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
