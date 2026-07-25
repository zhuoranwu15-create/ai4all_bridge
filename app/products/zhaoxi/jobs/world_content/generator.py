"""AI Feed 文字生成 port 与现有 LLM adapter。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent_runtime.llm.service import generate_completion
from app.agent_runtime.llm.providers import TASK_WORLD_CONTENT, tier_for_task


@dataclass(frozen=True)
class FeedGenerationRequest:
    """生成器所需最小公开上下文；不包含私聊原文、L3 或 runtime account。"""

    universe_id: str
    resident_id: str
    resident_name: str
    slot: str
    local_date: str


class FeedTextGenerator(Protocol):
    """可替换的世界动态文字生成端口。"""

    def generate(self, request: FeedGenerationRequest) -> str: ...


class LlmFeedTextGenerator:
    """使用现有后台 flash tier 生成一条简短、纯文字、无工具结果的动态。"""

    def generate(self, request: FeedGenerationRequest) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你为私人 AI 陪伴世界生成一条居民文字动态。"
                    "只输出一段自然中文正文，不要标题、Markdown、链接、工具结果或解释；"
                    "不虚构用户隐私、经历或现实事件，控制在 120 个汉字以内。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"居民名：{request.resident_name}\n"
                    f"时段：{request.slot}\n日期：{request.local_date}"
                ),
            },
        ]
        text = generate_completion(messages, tier=tier_for_task(TASK_WORLD_CONTENT))
        if not str(text or "").strip():
            raise RuntimeError("world content LLM returned empty text")
        return text
