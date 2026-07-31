"""Token 压力滚动摘要（P3，灰度默认关）。

后台维护「本会话已滑出短期窗口的头部消息」的滚动摘要，供下一轮 prompt 注入，避免长 session
旧消息滑出 100/token 窗口后静默丢失。设计见 docs/architecture/agent-runtime/context_window_token_budget_design.md §6。

与 carryover_summary（跨 session，session 轮转时由 dreaming 生成）语义不同：本摘要是 session **内**
的滚动压缩，按 token 压力 / 溢出条数触发，并存不互斥。

架构约束：
- 仅由 turn 后的后台链路调用（asyncio.to_thread），**不在用户同步回复链路执行 LLM 调用**。
- 幂等、容错：任何异常只记 warning，绝不影响用户回复。
- 水位线 rolling_summary_upto_id 单调推进，已摘要的消息不重复摘要。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.config import settings
from app.agent_runtime.context.window import ROLLING_SUMMARY_MAX_CHARS, compute_floor_count, estimate_tokens
from app.db import (
    get_session,
    list_context_messages_for_session,
    update_session_rolling_summary,
)
from app.time_utils import beijing_now

logger = logging.getLogger("ai4all.context_summarizer")

# 喂给摘要 LLM 的单条消息字符上限（逐条截断，避免个别超长消息撑大摘要请求）。
# 摘要产物字符上限直接用 context_window.ROLLING_SUMMARY_MAX_CHARS（单一真相源，与注入端同值）。
_SUMMARY_INPUT_PER_MSG_CHARS = 300

_SUMMARY_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。把较早的聊天记录压成简洁的中文摘要，供后续对话延续使用。"
    "只保留对后续陪伴有用的事实、状态、用户偏好、未完成的话题；"
    "去除寒暄与重复。不要编造，不要加入材料之外的信息。输出纯文本摘要，不要 JSON、不要标题。"
)


def _format_transcript(messages: List[Dict[str, Any]]) -> str:
    """把待压缩的整个 chunk 压成紧凑 transcript（**覆盖全部 chunk**、逐条截断）。

    不再按条数截尾——因为水位线会前移过整个 chunk，若这里丢掉最老几条，会造成"水位线覆盖 >
    实际进摘要"的信息缺口。逐条 300 字上限已足够约束单条超长；chunk 本身由 token 目标封顶。
    """
    lines: List[str] = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "AI"
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _SUMMARY_INPUT_PER_MSG_CHARS:
            content = content[:_SUMMARY_INPUT_PER_MSG_CHARS] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _deterministic_summary(transcript: str, prev_summary: str) -> str:
    """LLM 不可用时的确定性兜底：旧摘要 + 截断后的近期 transcript。"""
    parts = []
    if prev_summary:
        parts.append(prev_summary.strip())
    if transcript:
        parts.append("较早对话片段：\n" + transcript)
    text = "\n\n".join(parts).strip()
    return text[:ROLLING_SUMMARY_MAX_CHARS]


def _summarize(candidates: List[Dict[str, Any]], prev_summary: str) -> str:
    """把待摘要消息 + 旧滚动摘要合并蒸馏成新摘要文本（LLM，失败走确定性兜底）。"""
    transcript = _format_transcript(candidates)
    if not transcript and not prev_summary:
        return ""
    try:
        from app.agent_runtime.llm.service import generate_completion_with_usage, is_llm_configured
        from app.agent_runtime.llm.providers import TASK_ROLLING_SUMMARY, tier_for_task

        tier = tier_for_task(TASK_ROLLING_SUMMARY)
        if not is_llm_configured(tier):
            return _deterministic_summary(transcript, prev_summary)

        user_prompt = (
            "请把下面的内容合并成一段简洁的滚动摘要。\n\n"
            f"已有摘要（可能为空）：\n{prev_summary or '（无）'}\n\n"
            f"需要并入摘要的更早对话：\n{transcript or '（无）'}\n\n"
            "要求：在已有摘要基础上吸收新片段，保留关键事实与未完成话题，去重，"
            f"输出一段纯文本，不超过约 {ROLLING_SUMMARY_MAX_CHARS} 字。"
        )
        raw, _usage = generate_completion_with_usage(
            [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tier=tier,
        )
        text = str(raw or "").strip()
        # LLM 空响应与异常走同一确定性兜底。
        return (text or _deterministic_summary(transcript, prev_summary))[:ROLLING_SUMMARY_MAX_CHARS]
    except Exception:
        logger.warning("rolling summary LLM call failed; using deterministic fallback", exc_info=True)
        return _deterministic_summary(transcript, prev_summary)


def maybe_update_rolling_summary(*, account_id: str, session_id: int) -> Dict[str, Any]:
    """按需为本会话做「chunk 压缩」更新滚动摘要（后台调用，幂等、容错）。

    统一编排（token chunk + 水位线不变量）：
    1. 读水位线 rolling_summary_upto_id 与现有 rolling_summary。
    2. 尾窗 = 本 session「id > 水位线」的消息（ASC）——即组装期仍以原文喂给 LLM 的那部分。
    3. 尾窗 token ≤ 预算 → 无溢出，跳过（不压）。
    4. 否则一次压掉最老 `max(chunk_tokens, 溢出量)` token 对应的**整条消息**（但绝不碰硬底 F
       保护的最近原文）；把这批 merge 进 rolling、水位线前移过这批、落库。压完留出 headroom，
       下次要等尾窗重新涨过预算才再压 → 既不丢信息（组装期永不丢水位线之后的原文），又不必每轮调 LLM。

    返回处理状态 dict（仅供日志/调试）。开关关闭或任何异常都安全返回，不影响调用方。
    """
    if not getattr(settings, "llm_rolling_summary_enabled", False):
        return {"status": "disabled"}
    try:
        session = get_session(session_id=session_id)
        if not session or str(session.get("account_id")) != str(account_id):
            # 账号隔离硬校验：session 必须归属该账号，否则拒绝（防错误跨账号写入）。
            return {"status": "skip", "reason": "session_mismatch"}

        upto_id = int(session.get("rolling_summary_upto_id") or 0)
        prev_summary = (session.get("rolling_summary") or "").strip()
        budget = int(getattr(settings, "llm_context_token_budget", 0) or 0)
        if budget <= 0:
            # 压缩是预算驱动的：无 token 预算即无"溢出"概念，不压。
            return {"status": "skip", "reason": "no_budget"}

        # 尾窗 = 水位线之后的消息（ASC）——组装期以原文注入的那部分，也是唯一"未进摘要"的部分。
        tail = list_context_messages_for_session(
            session_id=session_id,
            account_id=account_id,
            after_id=upto_id,
        )
        if not tail:
            return {"status": "skip", "reason": "empty_tail"}
        # 每条 token 估算只算一次，供溢出判断与下方 chunk 累加复用。
        tail_tokens = [estimate_tokens(m.get("content")) for m in tail]
        raw_tokens = sum(tail_tokens)
        if raw_tokens <= budget:
            # 尾窗仍在预算内 → 无溢出，无需压缩。
            return {"status": "skip", "reason": "within_budget", "raw_tokens": raw_tokens}

        # 硬底：最近 floor_count 条原文永不被压缩（与组装期同口径，保护近场对话）。
        floor_count = compute_floor_count(
            tail,
            now=beijing_now(),
            floor_minutes=int(getattr(settings, "llm_context_floor_minutes", 15) or 0),
            floor_turns=int(getattr(settings, "llm_context_floor_turns", 10) or 0),
        )
        compressible_count = max(0, len(tail) - floor_count)
        if compressible_count == 0:
            # 溢出全落在硬底内（近场超预算）——不压，宁可短暂超预算（与组装期硬底优先一致）。
            return {"status": "skip", "reason": "all_within_floor", "raw_tokens": raw_tokens}

        # 一次压最老 max(chunk, 溢出量) token 对应的整条消息；压完留 headroom。
        chunk_tokens = max(1, int(getattr(settings, "rolling_summary_chunk_tokens", 1500) or 1500))
        target = max(chunk_tokens, raw_tokens - budget)
        gathered: List[Dict[str, Any]] = []
        acc = 0
        for i in range(compressible_count):
            gathered.append(tail[i])
            acc += tail_tokens[i]  # 复用上面已算的 per-row token
            if acc >= target:
                break

        new_summary = _summarize(gathered, prev_summary)
        if not new_summary:
            return {"status": "skip", "reason": "empty_summary"}

        new_upto = int(gathered[-1]["id"])
        update_session_rolling_summary(
            session_id=session_id,
            rolling_summary=new_summary,
            rolling_summary_upto_id=new_upto,
        )
        return {
            "status": "updated",
            "summarized": len(gathered),
            "upto_id": new_upto,
            "compressed_tokens": acc,
            "raw_tokens": raw_tokens,
        }
    except Exception:
        logger.warning(
            "rolling summary update failed account=%s session=%s",
            account_id,
            session_id,
            exc_info=True,
        )
        return {"status": "error"}
