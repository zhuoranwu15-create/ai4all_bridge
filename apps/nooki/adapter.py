"""Nooki Agent Adapter — Nooki 接入 AI4ALL Agent Platform 的实现。

职责：
- 把 Nooki 前端发来的 /agent/chat 请求翻译成标准 AgentContext
- 声明 Nooki 的 skill 搜索路径（apps/nooki/skills/）
- 加载人格持久化数据（Phase 2），如无持久化数据则回退到前端传入的 preamble
- 声明 Nooki 允许使用的平台能力

Context Contract 字段说明：
    identity:            用户基本信息（name 等）
    conversation:        对话历史（前端传入，最近 3 轮）
    memory:              长期记忆（Phase 3 接入 Memory Adapter 后填充）
    application_context: Nooki 业务状态，Core 不解读，原样透传给 LLM
    capabilities:        本次请求开启的平台能力

application_context 结构（由前端 ai.js 构建后传入）：
    preamble:    角色描述（Phase 2 前的过渡方案；Phase 2 后优先使用持久化人格）
    history:     最近 N 轮对话（与 conversation.recent_messages 同源）
    active_task: 进行中任务信息（Task Intelligence 使用）
    archetype:   用户选择的人设类型（gentle/calm/bestie/boss_review/palace_drama）
    其他字段透传，Core 不感知
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List

from sdk.adapter_base import AgentAdapter

if TYPE_CHECKING:
    from app.agent_runtime.models import AgentChatRequest, AgentContext

logger = logging.getLogger("nooki.adapter")

# Nooki skill 文件目录（优先于全局 skills/）
_SKILLS_DIR = Path(__file__).parent / "skills"


class NookiAdapter(AgentAdapter):
    """Nooki 应用 Adapter。

    Core 通过 adapter_registry 以 app="nooki" 查找并调用此 Adapter。
    Core 不直接 import NookiAdapter，不感知 Nooki 业务逻辑。
    """

    def build_context(self, request: "AgentChatRequest") -> "AgentContext":
        """把 Nooki 请求翻译成标准 AgentContext。

        Phase 2 增强：
        - 尝试从持久化文件加载人格（PersonaStorage）
        - 有持久化人格时：覆盖 preamble；顺带初始化首次用户（写入默认文件）
        - 无持久化人格时：回退到前端传入的 preamble（向后兼容 Phase 1 行为）
        - memory.relevant 从 Phase 3 接入 NookiMemoryAdapter 后填充
        """
        from app.agent_runtime.models import AgentContext  # 避免循环 import

        ctx = request.context or {}
        user_id = request.user_id or ""

        # Phase 2：尝试加载持久化人格
        preamble = ctx.get("preamble", "")
        if user_id:
            try:
                from apps.nooki.persona_storage import PersonaStorage
                storage = PersonaStorage(user_id)

                # 如果有 archetype 信息（首次初始化时存入）
                archetype = ctx.get("archetype", "")
                companion_name = ctx.get("companionName", "")
                if archetype:
                    storage.initialize_defaults(archetype=archetype, name=companion_name)

                # 优先使用持久化人格
                persisted_preamble = storage.build_preamble()
                if persisted_preamble:
                    preamble = persisted_preamble
            except Exception as exc:
                # 人格加载失败不阻断请求，降级到前端 preamble
                logger.warning("persona_storage load failed for user=%s: %s", user_id, exc)

        # Phase 3：尝试加载记忆（NookiMemoryAdapter）
        memory_relevant: list = []
        if user_id:
            try:
                from apps.nooki.memory_adapter import NookiMemoryAdapter
                mem_adapter = NookiMemoryAdapter(user_id)
                memory_relevant = mem_adapter.retrieve_relevant(ctx)
            except Exception as exc:
                logger.warning("memory_adapter load failed for user=%s: %s", user_id, exc)

        # 把 preamble 写回 ctx（供 prompt_builder 使用）
        business_ctx = dict(ctx)
        if preamble:
            business_ctx["preamble"] = preamble

        return AgentContext(
            user={"id": user_id},
            conversation={"recent_messages": ctx.get("history", [])},
            memory={"relevant": memory_relevant},
            business=business_ctx,
        )

    def get_skill_dirs(self) -> List[Path]:
        """Nooki skill 目录优先于全局 skills/。"""
        return [_SKILLS_DIR]

    def get_capabilities(self) -> List[str]:
        """Nooki 当前开启的平台能力。"""
        return ["task_engine", "timer", "persona_persistence", "memory"]


# ── 自注册 ──────────────────────────────────────────────────────────────────
# 只要此模块被 import，Nooki Adapter 就注册到 Core registry。
# Core 不主动 import 此文件，由 main.py startup 触发（见 app/main.py）。
from app.agent_runtime.adapter_registry import register as _register  # noqa: E402
_register("nooki", NookiAdapter())
