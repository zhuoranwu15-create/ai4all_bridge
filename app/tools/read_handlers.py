"""受控 read 工具 handler：只读 skills 白名单目录，供模型按需加载 SKILL.md。

设计约束：
- 只允许读取虚拟路径 'skills/<name>/...' 对应的 app/skills/ 下文件
- realpath containment check：防 symlink escape 和路径穿越
- 禁止读取 .env、数据库、日志、用户 profile、任意源码
- 按行数 + bytes 双截断，返回 continuation hint
"""
from pathlib import Path
from typing import Any, Dict

from app.skills import read_skill

# skills 虚拟路径前缀
_SKILLS_PREFIX = "skills/"

# 单次最大返回行数
_DEFAULT_LINE_LIMIT = 200
_MAX_LINE_LIMIT = 400


def handle_read(args: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """执行 read 工具调用。

    args:
        path     (str, required): 虚拟路径，如 'skills/weather/SKILL.md'
        offset   (int, optional): 从第几行开始（1-based），默认 1
        limit    (int, optional): 返回行数上限，默认 200，最大 400
    """
    path: str = (args.get("path") or "").strip()
    if not path:
        return {"status": "failed", "error": "path is required"}

    # 只允许 skills 前缀
    if not path.startswith(_SKILLS_PREFIX):
        from app.skills import list_skill_catalog
        available = [e["location"] for e in list_skill_catalog()]
        return {
            "status": "failed",
            "error": f"路径 {path!r} 不在允许的读取范围内。只允许读取 skills/ 前缀的文件。",
            "allowed_prefix": _SKILLS_PREFIX,
            "available": available,
        }

    content = read_skill(path)
    if content is None:
        from app.skills import list_skill_catalog
        available = [e["location"] for e in list_skill_catalog()]
        return {
            "status": "failed",
            "error": f"文件 {path!r} 不存在或不在允许的读取范围内",
            "available": available,
        }

    lines = content.splitlines()
    total_lines = len(lines)

    offset = int(args.get("offset") or 1)
    offset = max(1, offset)
    limit = int(args.get("limit") or _DEFAULT_LINE_LIMIT)
    limit = max(1, min(limit, _MAX_LINE_LIMIT))

    start = offset - 1  # 转 0-based
    end = start + limit
    selected = lines[start:end]
    truncated = end < total_lines

    result_text = "\n".join(selected)

    return {
        "status": "succeeded",
        "path": path,
        "totalLines": total_lines,
        "offset": offset,
        "limit": limit,
        "linesReturned": len(selected),
        "truncated": truncated,
        "continuationHint": (
            f"文件还有 {total_lines - end} 行未显示，可用 offset={end + 1} 继续读取。"
            if truncated else None
        ),
        "content": result_text,
    }
