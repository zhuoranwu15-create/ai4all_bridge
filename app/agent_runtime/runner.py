"""Agent Runner — 一次 Agent 请求的完整生命周期编排。

约束（永远不能违反）：
- 不调用 app/db/* 的任何写入函数
- 不调用 app/tools/* 的任何业务工具
- 不依赖 Legacy Chat 的任何模块（turn_service、prompt_builder 原文件等）
- 只通过 app/llm.generate_completion 调用 LLM
"""
import logging
import time
from typing import List, Optional

from app.agent_runtime import output_parser, prompt_builder
from app.agent_runtime.models import AgentChatRequest, AgentChatResponse, AgentContext
from app.agent_runtime.skill_registry import load_skills
from app.config import settings as _app_settings
from app.llm import generate_completion
from app.llm_providers import LLMProviderConfig

logger = logging.getLogger("ai4all.agent_runtime.runner")


def _agent_provider() -> Optional[LLMProviderConfig]:
    """读取 agent_llm_* settings 字段，返回专属 provider；未配置则返回 None（回退到全局默认）。"""
    base_url = (_app_settings.agent_llm_base_url or "").strip()
    model = (_app_settings.agent_llm_model or "").strip()
    api_key_env = (_app_settings.agent_llm_api_key_env or "").strip()
    if not (base_url and model and api_key_env):
        return None
    # api_key_env 如 "DASHSCOPE_API_KEY" → settings.dashscope_api_key
    attr = api_key_env.lower()
    api_key = (getattr(_app_settings, attr, "") or "").strip()
    if not api_key:
        return None
    return LLMProviderConfig(
        id="agent_llm",
        label=f"Agent LLM ({model})",
        protocol="openai_chat",
        base_url=base_url,
        model=model,
        api_key=api_key,
        family="agent",
        tier="pro",
        timeout_seconds=15.0,
    )


def run_agent(request: AgentChatRequest) -> AgentChatResponse:
    """执行一次 Agent 请求，返回结构化响应。

    流程：
      1. build_context   → AgentContext（结构化，不含 prompt 字符串）
      2. load_skills     → List[Skill]（filesystem allowlist，未知 skill 即报错）
      3. build_messages  → List[dict]（Agent 专用 prompt，与 Legacy 完全隔离）
      4. generate_completion → str（复用 app/llm，不修改该文件）
      5. parse           → dict（graceful fallback，不抛异常）
      6. return AgentChatResponse（recommendations 语义为建议，调用方决定执行）
    """
    t0 = time.monotonic()

    # 1. Context（内联自 context_builder）
    ctx = AgentContext(
        user={"id": request.user_id},
        conversation={"recent_messages": request.context.get("history", [])},
        memory={"relevant": []},
        business=request.context,
    )

    # 2. Skills（文件系统 allowlist，名称不存在即 ValueError → 由 router 转 400）
    skills: List = load_skills(request.skills) if request.skills else []

    # 3. Prompt（Agent 专用 prompt_builder，不污染 Legacy）
    messages = prompt_builder.build_messages(ctx, skills, request.message)

    # 4. LLM 调用（agent 专属 provider 优先，未配置则回退到全局默认）
    raw = generate_completion(messages, provider=_agent_provider())

    # 5. 解析（失败时 fallback，不抛异常）
    parsed = output_parser.parse(raw)

    # 6. 组装响应
    reply = parsed.get("reply") or ""
    understanding = {k: v for k, v in parsed.items() if k not in ("reply", "recommendations", "parse_error", "raw")}
    # recommendations 语义：建议，调用方决定是否执行，Agent 不执行任何业务状态变更
    recommendations = parsed.get("recommendations") or []

    latency_ms = int((time.monotonic() - t0) * 1000)
    metadata: dict = {"latency_ms": latency_ms}
    if parsed.get("parse_error"):
        metadata["parse_error"] = True

    logger.info(
        "agent_run app=%s user=%s skills=%s latency_ms=%d parse_error=%s",
        request.app,
        request.user_id,
        request.skills,
        latency_ms,
        parsed.get("parse_error", False),
    )

    return AgentChatResponse(
        reply=reply,
        understanding=understanding,
        recommendations=recommendations,
        metadata=metadata,
    )
