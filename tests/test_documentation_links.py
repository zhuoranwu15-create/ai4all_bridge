"""文档信息架构门禁：仓库内 Markdown 链接必须指向存在的文件。"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
DEPRECATED_DOC_REFERENCES = (
    "docs/tech_design/",
    "relationship_state_implementation_plan_tmp.md",
)


def _markdown_files() -> list[Path]:
    """返回受文档体系管理的 Markdown 文件。"""

    roots = [REPO_ROOT / "docs", REPO_ROOT / "patches"]
    files = [
        REPO_ROOT / name
        for name in ("README.md", "AGENTS.md", "CLAUDE.md")
        if (REPO_ROOT / name).is_file()
    ]
    for root in roots:
        if root.is_dir():
            files.extend(
                path
                for path in root.rglob("*.md")
                if "tmp" not in path.relative_to(root).parts
            )
    return sorted(set(files))


def _local_target(source: Path, raw_target: str) -> Path | None:
    """把仓库内相对链接解析为路径；外链、锚点与仓库外引用返回 None。"""

    target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
    if not target or target.startswith(("#", "/", "http://", "https://", "mailto:")):
        return None
    path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not path_text:
        return None
    resolved = (source.parent / path_text).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return resolved


def test_markdown_local_links_resolve():
    """移动文档时必须同步修复导航和正文中的仓库内链接。"""

    broken: list[str] = []
    for source in _markdown_files():
        content = source.read_text(encoding="utf-8")
        # 示例正则和 Markdown 片段中的 ``[x](y)`` 不是实际导航链接。
        content = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        content = re.sub(r"`[^`\n]*`", "", content)
        for match in MARKDOWN_LINK.finditer(content):
            target = _local_target(source, match.group(1))
            if target is not None and not target.exists():
                broken.append(
                    f"{source.relative_to(REPO_ROOT)} -> "
                    f"{match.group(1)}"
                )
    assert not broken, "失效的 Markdown 本地链接：\n" + "\n".join(broken)


def test_python_sources_do_not_reference_retired_document_paths():
    """代码说明必须引用当前文档 owner，不能重新引入已删除的旧目录。"""

    stale: list[str] = []
    this_file = Path(__file__).resolve()
    for root_name in ("app", "scripts", "tests", "nearline"):
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path.resolve() == this_file:
                continue
            content = path.read_text(encoding="utf-8")
            for marker in DEPRECATED_DOC_REFERENCES:
                if marker in content:
                    stale.append(f"{path.relative_to(REPO_ROOT)} -> {marker}")
    assert not stale, "仍在引用已退役文档路径：\n" + "\n".join(stale)
