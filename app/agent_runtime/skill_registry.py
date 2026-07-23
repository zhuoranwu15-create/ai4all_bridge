"""Skill 注册与加载。

Skill 以文件系统为 allowlist：
  skills/<name>/skill.md
  skills/<name>/schema.json

请求中的 skill 名只要目录不存在即报错，不存在目录遍历风险。
"""
import json
import logging
from pathlib import Path

from app.agent_runtime.models import Skill

logger = logging.getLogger("ai4all.agent_runtime.skill_registry")

# skills/ 目录位于项目根目录
_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


def load_skill(name: str) -> Skill:
    """加载指定 skill。

    Args:
        name: skill 名称，对应 skills/<name>/ 目录。

    Returns:
        Skill 对象。

    Raises:
        ValueError: skill 不存在（文件系统无对应目录或文件缺失）。
    """
    skill_dir = _SKILLS_DIR / name
    if not skill_dir.is_dir():
        raise ValueError(f"Unknown skill: {name!r}. Available skills are in {_SKILLS_DIR}")

    prompt_path = skill_dir / "skill.md"
    schema_path = skill_dir / "schema.json"

    if not prompt_path.exists():
        raise ValueError(f"Skill {name!r} is missing skill.md")
    if not schema_path.exists():
        raise ValueError(f"Skill {name!r} is missing schema.json")

    prompt = prompt_path.read_text(encoding="utf-8")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    version = schema.get("$version", "1.0")
    logger.debug("loaded skill %s v%s", name, version)
    return Skill(name=name, prompt=prompt, schema=schema, version=version)


def load_skills(names: list[str]) -> list[Skill]:
    """批量加载 skill 列表。任意一个不存在即抛 ValueError。"""
    return [load_skill(name) for name in names]
