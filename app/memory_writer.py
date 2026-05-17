import asyncio
import logging
from pathlib import Path
from typing import List, Dict

from app.config import settings
from app.user_profiles import _safe_account_dir_name

logger = logging.getLogger("ai4all.memory_writer")

EXTRACTION_PROMPT = """从以下对话片段中，提取值得长期记住的信息。
包括：用户明确说出的偏好、重要事件、情绪状态变化、
对我（AI）的反馈、明确的个人信息。
每条用 - 开头，一行一条，语言简洁。
如果没有值得记住的内容，输出 NOTHING。

对话：
{turns_text}"""


def memory_file_path(account_id: str, date_str: str) -> Path:
    """Return path to memory/YYYY-MM-DD.md for given account and date."""
    return (
        Path(settings.user_profiles_dir)
        / _safe_account_dir_name(account_id)
        / "memory"
        / f"{date_str}.md"
    )


def _format_turns(turns: List[Dict[str, str]]) -> str:
    """Format list of {role, content} dicts into readable conversation text."""
    lines = []
    for turn in turns:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            lines.append(f"用户：{content}")
        else:
            lines.append(f"AI：{content}")
    return "\n".join(lines)


def _extract_sync(turns: List[Dict[str, str]]) -> str:
    """Call LLM synchronously, return raw result string."""
    from app.llm import generate_reply

    if not settings.llm_api_key:
        return "NOTHING"

    turns_text = _format_turns(turns)
    prompt = EXTRACTION_PROMPT.format(turns_text=turns_text)

    try:
        return generate_reply(
            user_text=prompt,
            history=[],
            system_prompt="你是记忆提炼助手，从对话中提取有价值的信息，不需要解释你的工作。",
        )
    except Exception as exc:
        logger.warning("memory extraction LLM call failed: %s", exc)
        return "NOTHING"


def _append_to_memory(path: Path, content: str, date_str: str) -> None:
    """Create or append to the memory file. Adds a timestamp header if file is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {date_str}\n\n{content}", encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + content)


async def write_memory(
    account_id: str,
    turns: List[Dict[str, str]],
    today: str,
) -> None:
    """Extract memorable info from turns and append to daily memory file.

    Silently skips if no LLM key configured or if LLM returns NOTHING.
    Runs via asyncio.to_thread so it doesn't block the event loop.
    """
    try:
        if not turns:
            return

        result: str = await asyncio.to_thread(_extract_sync, turns)

        if result.strip().lower() == "nothing":
            logger.debug("memory_writer: LLM returned NOTHING, skipping write")
            return

        path = memory_file_path(account_id, today)
        _append_to_memory(path, result.strip(), today)
        logger.debug("memory_writer: wrote %d chars to %s", len(result), path)

    except Exception as exc:
        logger.exception("memory_writer: unexpected error: %s", exc)
