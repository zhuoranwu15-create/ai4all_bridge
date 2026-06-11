"""Structured 12-block prompt assembler for the AI4ALL WeChat bot."""
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional


logger = logging.getLogger("ai4all.prompt_builder")

# ---------------------------------------------------------------------------
# Load safety guardrail text once at module import time
# ---------------------------------------------------------------------------
_SAFETY_MD_PATH = Path(__file__).parent / "prompts" / "safety.md"
_MAX_SAFETY_CHARS = 8000

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

_OUTPUT_DIRECTIVES_FIXED = (
    "【回复格式要求】\n"
    "- 微信纯文本回复，不使用 Markdown 语法（不加 **粗体**、# 标题等）\n"
    "- 回复简洁，通常不超过 150 字，除非用户明确要求详细\n"
    "- 可适当使用 emoji 增加亲切感\n"
    "- 不要在回复末尾重复用户的问题"
)

_FACTUAL_DISCIPLINE = (
    "【事实准确】当前时间、日期、星期以下方运行时信息为准；"
    "涉及你和用户的关系事实（认识多久、连续聊天天数等）必须调用工具取真值，不要凭印象编造。"
)

_CONTEXT_BLOCK_ORDER = (
    "AGENTS",
    "TOOLS",
    "SOUL",
    "IDENTITY",
    "USER",
    "MEMORY",
)

_CONTEXT_BLOCK_LIMITS = {
    "AGENTS": 2000,
    "SOUL": 3000,
    "IDENTITY": 1500,
    "USER": 2000,
    "TOOLS": 3000,
    "MEMORY": 3000,
}


class PromptBuilder:
    """Assemble a system prompt from ordered prompt blocks.

    The stable prefix comes first for prompt-cache friendliness. Per-account
    project context and other volatile data are appended after that boundary.
    """

    def build(
        self,
        *,
        display_name: Optional[str] = None,
        soul: Optional[str] = None,
        user_prefs: Optional[str] = None,
        long_term_memory: Optional[str] = None,
        daily_notes: Optional[str] = None,
        carryover_summary: Optional[str] = None,
        system_prompt_override: Optional[str] = None,
        style: Optional[str] = None,
        tools: Optional[List[str]] = None,
        skills: Optional[List[str]] = None,
        agent_context: Optional[Dict[str, str]] = None,
        onboarding_context: Optional[str] = None,
        model_name: str = "",
        today: Optional[str] = None,
        current_time: Optional[str] = None,
        weekday: Optional[str] = None,
        daypart: Optional[str] = None,
        tool_instructions: Optional[str] = None,
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

        # Block 2: Safety
        if _SAFETY_TEXT:
            safety = _truncate(_SAFETY_TEXT, _MAX_SAFETY_CHARS, "safety")
            blocks.append(safety)

        # Block 3: Fixed output directives
        blocks.append(_OUTPUT_DIRECTIVES_FIXED)

        # Block 3b: Factual-accuracy discipline (time → runtime block; relationship → tool)
        blocks.append(_FACTUAL_DISCIPLINE)

        # Block 4: Skills
        if skills:
            skill_list = "、".join(skills)
            blocks.append(f"【技能列表】\n你擅长的领域包括：{skill_list}。")

        project_context = self._build_project_context(agent_context)
        if not project_context:
            # Legacy profile fallback. New accounts should use AGENTS/SOUL/etc.
            if display_name and display_name.strip():
                blocks.append(f"你的名字是 {display_name}。")

            if soul and soul.strip():
                soul_text = _truncate(soul, 3000, "soul")
                blocks.append(soul_text)

        # ------------------------------------------------------------------
        # VOLATILE BLOCKS (7-12) — change per user / per request
        # ------------------------------------------------------------------

        # Block 7: Project Context
        if project_context:
            blocks.append(project_context)

        # Block 8: Onboarding context (injected only during first-chat onboarding)
        if onboarding_context and onboarding_context.strip():
            blocks.append(onboarding_context.strip())

        # Block 9: User Preferences (legacy fallback)
        if not project_context and user_prefs and user_prefs.strip():
            user_prefs_text = _truncate(user_prefs, 2000, "user_prefs")
            blocks.append(f"【用户偏好】\n{user_prefs_text}")

        # Block 10: Long-term Memory (legacy fallback)
        if not project_context and long_term_memory and long_term_memory.strip():
            mem_text = _truncate(long_term_memory, 3000, "long_term_memory")
            blocks.append(f"【长期记忆】\n{mem_text}")

        # Block 11: Session Carryover
        if carryover_summary and carryover_summary.strip():
            carryover_text = _truncate(carryover_summary, 2000, "carryover_summary")
            blocks.append(f"【会话延续摘要】\n{carryover_text}")

        # Block 12: Daily Notes
        if daily_notes and daily_notes.strip():
            notes_text = _truncate(daily_notes, 2000, "daily_notes")
            blocks.append(f"【今日备注】\n{notes_text}")

        # Block 13: System Prompt Override
        if system_prompt_override and system_prompt_override.strip():
            blocks.append(f"【最高优先级覆盖指令】\n{system_prompt_override}")

        if style and style.strip():
            style_text = _truncate(style, 500, "style")
            blocks.append(f"【回复风格】\n- 当前用户偏好的回复风格：{style_text}")

        # Block 15: Runtime
        runtime_parts: List[str] = []
        if today and current_time:
            # Compact one-liner: date + weekday + time + day-part, e.g.
            # "现在是北京时间 2026-06-09 周一 14:30（下午）"
            wd = f" {weekday}" if weekday else ""
            dp = f"（{daypart}）" if daypart else ""
            runtime_parts.append(f"现在是北京时间 {today}{wd} {current_time}{dp}")
        elif today:
            wd = f" {weekday}" if weekday else ""
            runtime_parts.append(f"今天是 {today}{wd}")
        if model_name:
            runtime_parts.append(f"当前模型：{model_name}")
        if runtime_parts:
            blocks.append("【运行时信息】\n" + "\n".join(runtime_parts))

        # Block 16: Tool instructions (injected only for non-onboarding turns)
        if tool_instructions and tool_instructions.strip():
            blocks.append(tool_instructions.strip())

        return "\n\n".join(b for b in blocks if b)

    def _build_project_context(self, agent_context: Optional[Dict[str, str]]) -> str:
        if not agent_context:
            return ""

        sections: List[str] = []
        for key in _CONTEXT_BLOCK_ORDER:
            text = (agent_context.get(key) or "").strip()
            if not text:
                continue
            limit = _CONTEXT_BLOCK_LIMITS[key]
            body = _truncate(text, limit, f"context_{key.lower()}")
            sections.append(f"### {key}.md\n{body}")
        if not sections:
            return ""
        return "【Project Context】\n" + "\n\n".join(sections)
