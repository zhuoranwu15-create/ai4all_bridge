"""Prompt helpers for account companion type classification."""
from typing import Any, Dict, List

COMPANION_TYPE_ENUM = [
    "emotional_support",
    "life_reflection",
    "romance_roleplay",
    "practical_assistant",
    "learning_growth",
    "work_career",
    "parenting_family",
    "daily_chat",
    "content_explorer",
    "creative_expression",
]

COMPANION_CLASSIFY_PROMPT_TEMPLATE = """你是一个用户行为分析器，根据用户最近向 AI 发送的消息，从以下 10 种陪伴类型中判断该用户的使用场景。

## 类型定义
- emotional_support：倾诉、安慰、被理解
- life_reflection：价值观、选择、意义、长期规划
- romance_roleplay：恋爱/暧昧扮演，用户希望更亲密或角色化互动
- practical_assistant：任务、效率、信息整理
- learning_growth：学知识、练技能、求解释
- work_career：工作、创业、产品、管理
- parenting_family：育儿、家庭沟通、亲子关系
- daily_chat：日常闲聊、打发时间
- content_explorer：新闻、话题、兴趣内容
- creative_expression：写作、脑暴、角色设定、文案

## 要求
- 只依据用户侧消息判断，不参考 AI 的回复
- primary_type 必填，选最主要的 1 个
- secondary_types 可多选（0-3 个），选次要的
- confidence 反映你的确信程度（0.0-1.0）；消息少或信号混合时给低值
- reasoning 一句话，供内部审查，不展示给用户

## 用户最近消息（共 {n} 条）
{messages}

输出严格 JSON，不加其他文字：
{{"primary_type": "...", "secondary_types": [...], "confidence": 0.0, "reasoning": "..."}}
"""


def _clean_message_content(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 200:
        return text[:200] + "..."
    return text


def build_companion_classify_prompt(*, messages: List[Dict[str, Any]]) -> str:
    """接收入站消息列表，返回完整陪伴类型分类 prompt。"""
    lines = []
    for index, message in enumerate(messages, start=1):
        content = _clean_message_content(message.get("content"))
        if not content:
            continue
        lines.append(f"{index}. {content}")
    rendered_messages = "\n".join(lines) if lines else "（无可用用户消息）"
    return COMPANION_CLASSIFY_PROMPT_TEMPLATE.format(
        n=len(lines),
        messages=rendered_messages,
    )
