"""Nooki 对话入口；把 Nooki 产品服务显式注入通用 Agent Runtime。

Nooki 只有 App 渠道（小程序），没有 OpenClaw 微信入站，所以不需要朝夕那套
`build_*_channel_input_from_openclaw` / `handle_*_openclaw_turn` 包装。

FOCUS_TASK（当前聚焦任务/step 投影）不经 `ProductPromptContext.agent_context_blocks`——
那是 PromptBuilder 针对 SOUL/IDENTITY/USER/MEMORY/MISSION 的固定白名单，任意其它 key 会被
静默丢弃。这里改用 `ChannelTurnInput.extra_blocks`：Runtime 自身注释就把它标注为
"域层注入的外部 context 块（L3 等，ADR §7.3 接缝①）...form-B（App）入口由 platform
composition 读取并渲染 L3 后填入"，正是 Nooki 这种 App-only 产品该用的通用机制，不需要为此
扩 Runtime 白名单。
"""
from __future__ import annotations

from app.agent_runtime.context.prompt_builder import ContextBlock
from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn
from app.db import get_platform_user_id_for_account
from app.products.nooki.application.turn_services import NOOKI_TURN_SERVICES
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository


def _focus_task_block_text(platform_user_id: str) -> str:
    """把当前聚焦任务/step 投影成一段 FOCUS_TASK 文本；无任务时明确说没有。"""

    projection = GoalBreakdownService(SqlTaskRepository()).get_authoritative_state(platform_user_id)
    if projection.focus_task is None:
        return "## FOCUS_TASK\n用户目前没有进行中的任务。\n"

    task = projection.focus_task
    lines = [
        "## FOCUS_TASK",
        f"task_id={task.id}，任务：{task.title}（原始诉求：{task.raw_goal}），状态：{task.status}",
    ]
    if projection.current_step is not None:
        step = projection.current_step
        lines.append(f"当前 step：step_id={step.id}，{step.title}（{step.status}）")
    if projection.plans:
        options = "；".join(f"plan_id={p.id} {p.mode}={p.title}" for p in projection.plans)
        lines.append(f"待选方案：{options}")
    if projection.active_tasks_count > 1:
        lines.append(f"用户还有其他 {projection.active_tasks_count - 1} 个进行中任务未在此展示。")
    return "\n".join(lines) + "\n"


def run_nooki_turn(ctx: ChannelTurnInput):
    """执行一次显式绑定 Nooki 产品服务的 turn；先把 FOCUS_TASK 域层块注入 `ctx.extra_blocks`。"""

    platform_user_id = get_platform_user_id_for_account(account_id=ctx.account_id)
    if platform_user_id:
        focus_text = _focus_task_block_text(platform_user_id)
        ctx.extra_blocks = [
            *ctx.extra_blocks,
            ContextBlock(name="nooki_focus_task", text=focus_text, trim_priority=70),
        ]
    return run_product_turn(ctx, product_services=NOOKI_TURN_SERVICES)


__all__ = ["run_nooki_turn"]
