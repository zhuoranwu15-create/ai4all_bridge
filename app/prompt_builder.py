"""Structured 12-block prompt assembler for the AI4ALL WeChat bot.

组装采用声明式 block 流水线：build/assemble 产出一组 ``ContextBlock``（每个携带 name、
section[stable|volatile]、char_limit、trim_priority），再按 token 预算裁剪（默认关闭）并 join。
``build()`` 仍返回纯字符串（向后兼容）；``assemble()`` 额外返回每个 block 的元数据，供调用方
直接读取，免去手维护 ``*_chars`` / 僵尸 ``daily_notes_loaded`` 并避免与真实组装漂移。
"""
import logging
import math
import re
from dataclasses import dataclass, field
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


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for budget trimming only (NOT for billing).

    中文约 1 token/字、英文/混排约 1 token/1.5 字；取 len/1.5 的 ceil 作保守近似。
    精度不要求高——预算裁剪本身是粗粒度的兜底，真值计量走 LLM usage。
    """
    return math.ceil(len(text or "") / 1.5)


# ---------------------------------------------------------------------------
# Block model
# ---------------------------------------------------------------------------

_SECTION_STABLE = "stable"      # 缓存友好前缀：安全/输出纪律等，永不被预算裁剪
_SECTION_VOLATILE = "volatile"  # 按用户/请求变化，预算超限时按 trim_priority 从低到高丢弃


@dataclass
class ContextBlock:
    """一个组装单元。text 已是最终（截断/带标记）文本，join 时原样拼接。"""
    name: str
    text: str
    section: str = _SECTION_VOLATILE
    char_limit: Optional[int] = None  # 仅作元数据记录；截断在加入前已应用
    trim_priority: int = 50           # 越小越先被预算裁剪（仅 volatile 生效）


@dataclass
class BlockMetric:
    name: str
    section: str
    chars: int
    included: bool


@dataclass
class BuildResult:
    """assemble() 的结果：最终 prompt 字符串 + 每个已组装 block 的元数据。"""
    prompt: str
    blocks: List[BlockMetric] = field(default_factory=list)

    def included(self, name: str) -> bool:
        return any(b.name == name and b.included for b in self.blocks)

    def final_chars(self, name: str) -> int:
        for b in self.blocks:
            if b.name == name and b.included:
                return b.chars
        return 0

    def as_dict(self) -> Dict[str, Dict[str, object]]:
        return {
            b.name: {"chars": b.chars, "section": b.section, "included": b.included}
            for b in self.blocks
        }


def _apply_token_budget(blocks: List[ContextBlock], token_budget: Optional[int]) -> List[ContextBlock]:
    """超预算时按 trim_priority 从低到高丢弃 volatile block；stable 永不丢弃。

    token_budget=None（默认）直接原样返回——零行为变更。返回保持原顺序的"保留"子集。
    """
    if token_budget is None:
        return list(blocks)
    total = sum(_estimate_tokens(b.text) for b in blocks)
    if total <= token_budget:
        return list(blocks)
    dropped: set = set()
    volatile_low_first = sorted(
        (b for b in blocks if b.section == _SECTION_VOLATILE),
        key=lambda b: b.trim_priority,
    )
    for b in volatile_low_first:
        if total <= token_budget:
            break
        dropped.add(id(b))
        total -= _estimate_tokens(b.text)
        logger.warning(
            "prompt block %s dropped for token budget (priority=%d)", b.name, b.trim_priority
        )
    return [b for b in blocks if id(b) not in dropped]


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
    "涉及你和用户的关系事实（认识多久、连续聊天天数等）必须调用 session_status 等可用工具取真值，"
    "不要凭印象编造。"
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
        extra_blocks: Optional[List[ContextBlock]] = None,
        token_budget: Optional[int] = None,
    ) -> str:
        """Build and return the assembled system prompt string (向后兼容入口)。"""
        return self.assemble(
            display_name=display_name,
            soul=soul,
            user_prefs=user_prefs,
            long_term_memory=long_term_memory,
            daily_notes=daily_notes,
            carryover_summary=carryover_summary,
            system_prompt_override=system_prompt_override,
            style=style,
            tools=tools,
            skills=skills,
            agent_context=agent_context,
            onboarding_context=onboarding_context,
            model_name=model_name,
            today=today,
            current_time=current_time,
            weekday=weekday,
            daypart=daypart,
            tool_instructions=tool_instructions,
            extra_blocks=extra_blocks,
            token_budget=token_budget,
        ).prompt

    def assemble(
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
        extra_blocks: Optional[List[ContextBlock]] = None,
        token_budget: Optional[int] = None,
    ) -> BuildResult:
        """组装为声明式 block 列表，应用 token 预算后 join；返回 prompt + block 元数据。

        block 顺序、标记文案、各 block 字符截断阈值与历史完全一致：token_budget=None（默认）时
        输出逐字不变。extra_blocks 在内置 block 之后追加（动态来源注入钩子）。
        """
        blocks: List[ContextBlock] = []

        def _add(
            name: str,
            text: str,
            *,
            section: str = _SECTION_VOLATILE,
            char_limit: Optional[int] = None,
            trim_priority: int = 50,
        ) -> None:
            if not text:
                return
            blocks.append(
                ContextBlock(
                    name=name,
                    text=text,
                    section=section,
                    char_limit=char_limit,
                    trim_priority=trim_priority,
                )
            )

        # ------------------------------------------------------------------
        # STABLE BLOCKS (1-6) — cache-friendly; rarely change
        # ------------------------------------------------------------------

        # Block 1: Tooling
        if tools:
            tool_list = "、".join(tools)
            _add("tooling", f"【可用工具】\n你可以调用以下工具：{tool_list}。", section=_SECTION_STABLE)

        # Block 2: Safety
        if _SAFETY_TEXT:
            safety = _truncate(_SAFETY_TEXT, _MAX_SAFETY_CHARS, "safety")
            _add("safety", safety, section=_SECTION_STABLE, char_limit=_MAX_SAFETY_CHARS)

        # Block 3: Fixed output directives
        _add("output_directives", _OUTPUT_DIRECTIVES_FIXED, section=_SECTION_STABLE)

        # Block 3b: Factual-accuracy discipline (time → runtime block; relationship → tool)
        _add("factual_discipline", _FACTUAL_DISCIPLINE, section=_SECTION_STABLE)

        # Block 4: Skills
        if skills:
            skill_list = "、".join(skills)
            _add("skills", f"【技能列表】\n你擅长的领域包括：{skill_list}。", section=_SECTION_STABLE)

        project_context = self._build_project_context(agent_context)
        if not project_context:
            # Legacy profile fallback. New accounts should use AGENTS/SOUL/etc.
            if display_name and display_name.strip():
                _add("legacy_identity", f"你的名字是 {display_name}。", section=_SECTION_STABLE)

            if soul and soul.strip():
                soul_text = _truncate(soul, 3000, "soul")
                _add("legacy_soul", soul_text, section=_SECTION_STABLE, char_limit=3000)

        # ------------------------------------------------------------------
        # VOLATILE BLOCKS (7-12) — change per user / per request
        # ------------------------------------------------------------------

        # Block 7: Project Context
        if project_context:
            _add("project_context", project_context, trim_priority=80)

        # Block 8: Onboarding context (injected only during first-chat onboarding)
        if onboarding_context and onboarding_context.strip():
            _add("onboarding_context", onboarding_context.strip(), trim_priority=90)

        # Block 9: User Preferences (legacy fallback)
        if not project_context and user_prefs and user_prefs.strip():
            user_prefs_text = _truncate(user_prefs, 2000, "user_prefs")
            _add("legacy_user_prefs", f"【用户偏好】\n{user_prefs_text}", char_limit=2000, trim_priority=35)

        # Block 10: Long-term Memory (legacy fallback)
        if not project_context and long_term_memory and long_term_memory.strip():
            mem_text = _truncate(long_term_memory, 3000, "long_term_memory")
            _add("legacy_long_term_memory", f"【长期记忆】\n{mem_text}", char_limit=3000, trim_priority=30)

        # Block 11: Session Carryover
        if carryover_summary and carryover_summary.strip():
            carryover_text = _truncate(carryover_summary, 2000, "carryover_summary")
            _add("carryover_summary", f"【会话延续摘要】\n{carryover_text}", char_limit=2000, trim_priority=20)

        # Block 12: Daily Notes
        if daily_notes and daily_notes.strip():
            notes_text = _truncate(daily_notes, 2000, "daily_notes")
            _add("daily_notes", f"【今日备注】\n{notes_text}", char_limit=2000, trim_priority=10)

        # Block 13: System Prompt Override
        if system_prompt_override and system_prompt_override.strip():
            _add("system_prompt_override", f"【最高优先级覆盖指令】\n{system_prompt_override}", trim_priority=100)

        if style and style.strip():
            style_text = _truncate(style, 500, "style")
            _add("style", f"【回复风格】\n- 当前用户偏好的回复风格：{style_text}", char_limit=500, trim_priority=40)

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
            _add("runtime", "【运行时信息】\n" + "\n".join(runtime_parts), trim_priority=70)

        # Block 16: Tool instructions (injected only for non-onboarding turns)
        if tool_instructions and tool_instructions.strip():
            _add("tool_instructions", tool_instructions.strip(), trim_priority=60)

        # 动态来源注入钩子：在内置 block 之后追加（默认 None = 无影响）。
        for blk in extra_blocks or []:
            if blk and blk.text:
                blocks.append(blk)

        kept = _apply_token_budget(blocks, token_budget)
        kept_ids = {id(b) for b in kept}
        prompt = "\n\n".join(b.text for b in kept if b.text)
        metrics = [
            BlockMetric(name=b.name, section=b.section, chars=len(b.text), included=id(b) in kept_ids)
            for b in blocks
        ]
        return BuildResult(prompt=prompt, blocks=metrics)

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
