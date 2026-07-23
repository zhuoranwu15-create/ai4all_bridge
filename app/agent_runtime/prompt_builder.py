"""Agent Runtime Prompt Builder。

独立于 app/prompt_builder.py（Legacy Chat 专用），完全不复用、不污染。
输入：AgentContext + List[Skill]
输出：messages 列表（可直接传给 generate_completion）
"""
import json
from typing import List

from app.agent_runtime.models import AgentContext, Skill


def build_messages(
    context: AgentContext,
    skills: List[Skill],
    user_message: str,
) -> List[dict]:
    """组装 LLM messages 列表。

    prompt 结构：
      [system] （preamble 或 Agent 身份）+ skills + 输出约束 + （无 preamble 时的上下文）
      [history] 对话历史（来自 context.business.history）
      [user]    用户消息
    """
    system_content = _build_system(context, skills)
    history = context.conversation.get("recent_messages", [])
    return [
        {"role": "system", "content": system_content},
        *history,
        {"role": "user", "content": user_message},
    ]


def _build_system(context: AgentContext, skills: List[Skill]) -> str:
    """构建 system prompt。

    有 preamble 时：preamble（角色+用户状态）+ skill 规则 + 输出格式
    无 preamble 时：默认 Agent 身份 + skill 规则 + 输出格式 + 上下文摘要
    """
    preamble = (context.business or {}).get("preamble", "")

    parts: List[str] = []

    # 1. 输出格式约束（最先声明，优先级最高）
    if skills:
        primary_skill = skills[0]
        parts.append(
            "【强制要求】你的每一次回复必须是且仅是一个合法的 JSON 对象，不得包含任何 markdown 代码块、"
            "解释文字或 JSON 对象之外的任何内容。违反此规则视为错误输出。\n\n"
            "输出 schema：\n"
            f"{json.dumps(primary_skill.schema, ensure_ascii=False, indent=2)}"
        )
    else:
        parts.append('【强制要求】只输出 JSON 对象：{"reply": "<你的回复>"}，不得有任何其他内容。')

    # 2. 身份/前置描述
    if preamble:
        parts.append(preamble)
    else:
        parts.append(
            "你是 AI4ALL Agent。理解用户意图，返回结构化 JSON。"
            "不执行任何业务动作，输出只含理解结果和建议，调用方决定是否执行。"
        )

    # 3. Skill 规则（直接追加 skill.md 内容，不加 XML 包裹，避免影响中文 prompt 结构）
    for skill in skills:
        parts.append(skill.prompt)

    # 4. 输出格式再次强调（放在末尾增强记忆）
    if skills:
        parts.append(
            "再次强调：只输出 JSON 对象本身，不加任何 markdown 标记或额外文字。"
        )
    else:
        parts.append('## Output Format\nReturn ONLY valid JSON: {"reply": "<your response>"}')

    # 5. 上下文摘要（仅无 preamble 时附加，避免与业务侧已注入的上下文重复）
    if not preamble and context.business:
        # 过滤掉 history/preamble 字段，只展示业务上下文
        biz = {k: v for k, v in context.business.items() if k not in ("history", "preamble")}
        if biz:
            parts.append(f"## Context\n- User ID: {context.user.get('id', 'unknown')}\n"
                         f"- Business context: {json.dumps(biz, ensure_ascii=False)}")

    return "\n\n".join(parts)
