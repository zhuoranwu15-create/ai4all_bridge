"""Skill catalog：发现并索引 app/skills/ 下的 skill 文件。

约定：
- 每个 skill 对应一个子目录 app/skills/<name>/
- 目录下必须有 SKILL.md（大写），否则不被识别
- skill name 只允许 [a-z0-9_-]+
- catalog 在模块级别按需缓存（进程生命周期内不变）
"""
import hashlib
import re
from pathlib import Path
from typing import Optional

_SKILLS_DIR = Path(__file__).parent
_VALID_NAME = re.compile(r"^[a-z0-9_-]+$")

# 模块级缓存；进程内 skill 文件不会热更新
_catalog_cache: Optional[list] = None


def _extract_summary(content: str) -> str:
    """从 SKILL.md 中提取 ## summary 节的第一段文字。"""
    match = re.search(
        r"##\s+summary\s*\n(.*?)(?=\n##|\Z)",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip().splitlines()[0].strip()
    # fallback：取第一个非空行
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    return ""


def _skill_version(content: str) -> str:
    """用内容 SHA-256 前 8 字节作为 version 标识。"""
    digest = hashlib.sha256(content.encode()).hexdigest()
    return digest[:16]


def list_skill_catalog() -> list:
    """返回所有可用 skill 的 catalog 列表（模块级缓存）。

    每项结构：
    {
        "name": "weather",
        "description": "...",
        "location": "skills/weather/SKILL.md",   # read 工具使用的虚拟路径
        "version": "abcd1234ef567890",
    }
    """
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache

    catalog = []
    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        name = skill_dir.name
        if name.startswith("_") or not _VALID_NAME.match(name):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        content = skill_md.read_text(encoding="utf-8")
        catalog.append(
            {
                "name": name,
                "description": _extract_summary(content),
                "location": f"skills/{name}/SKILL.md",
                "version": _skill_version(content),
            }
        )

    _catalog_cache = catalog
    return catalog


def read_skill(virtual_path: str) -> Optional[str]:
    """读取 skill 文件内容（供 read handler 调用）。

    virtual_path 必须以 'skills/' 开头且落在 app/skills/ 内；否则返回 None。
    """
    if not virtual_path.startswith("skills/"):
        return None
    relative = virtual_path[len("skills/"):]
    real = (_SKILLS_DIR / relative).resolve()
    skills_root = _SKILLS_DIR.resolve()
    try:
        real.relative_to(skills_root)
    except ValueError:
        return None  # 路径穿越
    if not real.exists() or not real.is_file():
        return None
    return real.read_text(encoding="utf-8")
