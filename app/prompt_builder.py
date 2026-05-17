"""Structured 12-block prompt assembler for the AI4ALL WeChat bot."""
import logging
import re
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger("ai4all.prompt_builder")

# ---------------------------------------------------------------------------
# Load safety guardrail text once at module import time
# ---------------------------------------------------------------------------
_SAFETY_MD_PATH = Path(__file__).parent / "prompts" / "safety.md"
_MAX_SAFETY_CHARS = 1000

try:
    _SAFETY_TEXT = _SAFETY_MD_PATH.read_text(encoding="utf-8").strip()
except FileNotFoundError:
    logger.warning("safety.md not found at %s; safety block will be empty", _SAFETY_MD_PATH)
    _SAFETY_TEXT = ""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def extract_section(markdown: str, section_name: str) -> str:
    """Extract content of a ## section_name heading from markdown.

    Returns the text between ``## section_name`` and the next ``##`` heading
    (or end of string).  Strips surrounding whitespace.  Returns empty string
    if the section is not found.
    """
    # Match the heading line followed by all content until the next ## heading
    pattern = r"(?m)^##\s+" + re.escape(section_name) + r"\s*\n(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, markdown, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _truncate(text: str, limit: int, block_name: str) -> str:
    """Truncate *text* to *limit* chars, appending ``...[已截断]`` if trimmed."""
    if len(text) <= limit:
        return text
    marker = "...[已截断]"
    truncated = text[:limit] + marker
    logger.warning(
        "prompt block %s truncated: %d -> %d chars", block_name, len(text), limit
    )
    return truncated


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

_EXECUTION_BIAS = (
    "请积极、主动地提供帮助，保持回复紧扣话题。"
    "遇到不清晰的输入时，礼貌地请求澄清，或根据上下文给出合理推断，而非直接拒绝。"
)

_OUTPUT_DIRECTIVES_FIXED = (
    "【回复格式要求】\n"
    "- 微信纯文本回复，不使用 Markdown 语法（不加 **粗体**、# 标题等）\n"
    "- 回复简洁，通常不超过 150 字，除非用户明确要求详细\n"
    "- 可适当使用 emoji 增加亲切感\n"
    "- 不要在回复末尾重复用户的问题"
)


class PromptBuilder:
    """Assemble a system prompt from up to 12 ordered blocks.

    Blocks 1-6 are stable (suitable for prompt caching).
    Blocks 7-12 are volatile (change per request or per user session).
    """

    def build(
        self,
        *,
        display_name: Optional[str] = None,
        soul: Optional[str] = None,
        user_prefs: Optional[str] = None,
        long_term_memory: Optional[str] = None,
        daily_notes: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        style: Optional[str] = None,
        tools: Optional[List[str]] = None,
        skills: Optional[List[str]] = None,
        model_name: str = "",
        today: Optional[str] = None,
    ) -> str:
        """Build and return the assembled system prompt string."""
        blocks: List[str] = []

        # ------------------------------------------------------------------
        # STABLE BLOCKS (1-6) — cache-friendly; rarely change
        # ------------------------------------------------------------------

        # Block 1: Tooling
        if tools:
            tool_list = "、".join(tools)
            blocks.append(f"【可用工具】\n你可以调用以下工具：{tool_list}。")

        # Block 2: Execution Bias
        blocks.append(_EXECUTION_BIAS)

        # Block 3: Safety
        if _SAFETY_TEXT:
            safety = _truncate(_SAFETY_TEXT, _MAX_SAFETY_CHARS, "safety")
            blocks.append(safety)

        # Block 4: Identity
        if display_name and display_name.strip():
            blocks.append(f"你的名字是 {display_name}。")

        # Block 5: Soul
        if soul and soul.strip():
            soul_text = _truncate(soul, 3000, "soul")
            blocks.append(soul_text)

        # Block 6: Skills
        if skills:
            skill_list = "、".join(skills)
            blocks.append(f"【技能列表】\n你擅长的领域包括：{skill_list}。")

        # ------------------------------------------------------------------
        # VOLATILE BLOCKS (7-12) — change per user / per request
        # ------------------------------------------------------------------

        # Block 7: User Preferences
        if user_prefs and user_prefs.strip():
            user_prefs_text = _truncate(user_prefs, 2000, "user_prefs")
            blocks.append(f"【用户偏好】\n{user_prefs_text}")

        # Block 8: Long-term Memory
        if long_term_memory and long_term_memory.strip():
            mem_text = _truncate(long_term_memory, 3000, "long_term_memory")
            blocks.append(f"【长期记忆】\n{mem_text}")

        # Block 9: Daily Notes
        if daily_notes and daily_notes.strip():
            notes_text = _truncate(daily_notes, 2000, "daily_notes")
            blocks.append(f"【今日备注】\n{notes_text}")

        # Block 10: System Prompt Override
        if system_prompt_override and system_prompt_override.strip():
            blocks.append(f"【最高优先级覆盖指令】\n{system_prompt_override}")

        # Block 11: Output Directives
        directives = _OUTPUT_DIRECTIVES_FIXED
        if style and style.strip():
            style_text = _truncate(style, 500, "style")
            directives = directives + f"\n- 当前用户偏好的回复风格：{style_text}"
        blocks.append(directives)

        # Block 12: Runtime
        runtime_parts: List[str] = []
        if today:
            runtime_parts.append(f"当前日期：{today}")
        if model_name:
            runtime_parts.append(f"当前模型：{model_name}")
        if runtime_parts:
            blocks.append("【运行时信息】\n" + "\n".join(runtime_parts))

        return "\n\n".join(b for b in blocks if b)
