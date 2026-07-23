"""Agent Runtime 数据模型。

与 Legacy Chat 完全隔离，不复用 app/schemas.py。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    """一个可插拔能力单元。"""
    name: str
    prompt: str       # skill.md 内容，直接注入 system prompt
    schema: dict      # output schema（来自 schema.json）
    version: str = "1.0"


# ---------------------------------------------------------------------------
# Agent Context — 结构化上下文，由 context_builder 生成，由 prompt_builder 消费
# 不直接生成 prompt 字符串
# ---------------------------------------------------------------------------

@dataclass
class AgentContext:
    """给 LLM 的结构化上下文。

    prompt_builder 负责将此对象格式化为 prompt 片段。
    context_builder 负责填充各字段，与业务无关。
    """
    user: Dict[str, Any] = field(default_factory=dict)
    """用户基础信息，如 {"id": "xxx"}。"""

    conversation: Dict[str, Any] = field(default_factory=dict)
    """近期对话，如 {"recent_messages": [...]}。Phase 1 为空。"""

    memory: Dict[str, Any] = field(default_factory=dict)
    """相关记忆，如 {"relevant": [...]}。Phase 1 为空。"""

    business: Dict[str, Any] = field(default_factory=dict)
    """业务侧透传的状态，如 {"app": "nooki", "state": {...}}。Agent 不解读，原样传递。"""


# ---------------------------------------------------------------------------
# HTTP Request / Response
# ---------------------------------------------------------------------------

class AgentChatRequest(BaseModel):
    app: str = Field(..., description="调用方应用标识，如 'nooki'")
    user_id: str = Field(..., description="用户 ID")
    message: str = Field(..., description="用户消息")
    context: Dict[str, Any] = Field(default_factory=dict, description="业务侧透传上下文，Agent 原样传递")
    skills: List[str] = Field(default_factory=list, description="本次请求激活的 skill 列表")


class AgentChatResponse(BaseModel):
    reply: str = Field(..., description="对用户的自然语言回复")
    understanding: Dict[str, Any] = Field(
        default_factory=dict,
        description="结构化意图理解结果（来自激活 skill 的输出）",
    )
    recommendations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Agent 对业务方的建议列表。"
            "这些是建议（suggestions only），调用方自行决定是否执行。"
            "Agent 不执行任何业务状态变更。"
        ),
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="调试 / 追踪元数据")
