"""Token 压力滚动摘要（P3，灰度默认关）。

后台维护「本会话已滑出短期窗口的头部消息」的滚动摘要，供下一轮 prompt 注入，避免长 session
旧消息滑出 100/token 窗口后静默丢失。设计见 docs/tech_design/context_window_token_budget_design.md §6。

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
from app.context_window import trim_history_rows
from app.db import (
    get_session,
    list_context_messages_for_session,
    list_recent_messages_for_account,
    update_session_rolling_summary,
)

logger = logging.getLogger("ai4all.context_summarizer")

# 喂给摘要 LLM 的单条消息字符上限与最多条数，避免摘要请求本身过大。
_SUMMARY_INPUT_PER_MSG_CHARS = 300
_SUMMARY_INPUT_MAX_MSGS = 80
# 摘要产物字符上限（与 prompt_builder 注入 block 的 2000 截断对齐，留余量）。
_SUMMARY_OUTPUT_MAX_CHARS = 1000

_SUMMARY_SYSTEM_PROMPT = (
    "你是对话记忆压缩器。把较早的聊天记录压成简洁的中文摘要，供后续对话延续使用。"
    "只保留对后续陪伴有用的事实、状态、用户偏好、未完成的话题；"
    "去除寒暄与重复。不要编造，不要加入材料之外的信息。输出纯文本摘要，不要 JSON、不要标题。"
)


def _format_transcript(messages: List[Dict[str, Any]]) -> str:
    """把消息列表压成紧凑 transcript（取尾部 N 条、逐条截断）。"""
    tail = messages[-_SUMMARY_INPUT_MAX_MSGS:]
    lines: List[str] = []
    for m in tail:
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
    return text[:_SUMMARY_OUTPUT_MAX_CHARS]


def _summarize(candidates: List[Dict[str, Any]], prev_summary: str) -> str:
    """把待摘要消息 + 旧滚动摘要合并蒸馏成新摘要文本（LLM，失败走确定性兜底）。"""
    transcript = _format_transcript(candidates)
    if not transcript and not prev_summary:
        return ""
    try:
        from app.llm import generate_completion_with_usage, is_llm_configured

        if not is_llm_configured():
            return _deterministic_summary(transcript, prev_summary)

        user_prompt = (
            "请把下面的内容合并成一段简洁的滚动摘要。\n\n"
            f"已有摘要（可能为空）：\n{prev_summary or '（无）'}\n\n"
            f"需要并入摘要的更早对话：\n{transcript or '（无）'}\n\n"
            "要求：在已有摘要基础上吸收新片段，保留关键事实与未完成话题，去重，"
            f"输出一段纯文本，不超过约 {_SUMMARY_OUTPUT_MAX_CHARS} 字。"
        )
        raw, _usage = generate_completion_with_usage(
            [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
        text = str(raw or "").strip()
        # LLM 空响应与异常走同一确定性兜底。
        return (text or _deterministic_summary(transcript, prev_summary))[:_SUMMARY_OUTPUT_MAX_CHARS]
    except Exception:
        logger.warning("rolling summary LLM call failed; using deterministic fallback", exc_info=True)
        return _deterministic_summary(transcript, prev_summary)


def maybe_update_rolling_summary(*, account_id: str, session_id: int) -> Dict[str, Any]:
    """按需为本会话生成/更新滚动摘要（后台调用，幂等、容错）。

    流程：
    1. 读 session 的水位线 rolling_summary_upto_id 与现有 rolling_summary。
    2. 用与同步链路**完全相同**的口径重算「live window 最旧 id」作为溢出分界：
       取该账号最近 llm_context_messages 条，经 trim_history_rows（token 预算 + 单条上限）
       裁剪后，kept 中最小 id 即 prompt 仍在喂的最旧消息；id < 该分界的本会话消息即已溢出。
       （修复：旧实现按 llm_context_messages 条数判溢出，与同步的 token 预算裁剪不一致——
       被 token 预算丢掉、但条数未超的消息会漏摘要。）
    3. 待摘要候选 = 本会话中「id > 水位线」且「id < live window 最旧 id」的消息；
       查询以 after_id=水位线 分页推进（修复：旧实现 after_id=0 固定取头 1000 条，
       session 超 1000 条后水位线卡死、后续真实溢出永不被摘要）。
    4. 候选条数 < 触发阈值 → 跳过；否则摘要、推进水位线、落库。

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

        # 与 build_turn_llm_input 同口径重算 live window：account-scoped 取数 + 相同预算裁剪。
        recent_rows = list_recent_messages_for_account(
            account_id=account_id,
            limit=max(1, int(getattr(settings, "llm_context_messages", 100) or 100)),
        )
        kept_rows = trim_history_rows(
            recent_rows,
            token_budget=int(getattr(settings, "llm_context_token_budget", 0) or 0),
            per_message_max_chars=int(getattr(settings, "llm_context_message_max_chars", 0) or 0),
        )["kept"]
        if not kept_rows:
            # 账号无近期消息：无 live window，无可摘要的溢出。
            return {"status": "skip", "reason": "below_window", "total": 0}
        # live window 最旧 id：仍在 prompt 中的最早消息 id；本会话中早于它的即已溢出。
        window_oldest_id = min(int(r["id"]) for r in kept_rows)

        # 从水位线分页取本会话候选，再以 live window 分界裁掉仍在窗口内的消息。
        session_msgs = list_context_messages_for_session(session_id=session_id, after_id=upto_id)
        candidates = [m for m in session_msgs if int(m["id"]) < window_oldest_id]
        if not candidates:
            # 本会话尚无新溢出消息（全部仍在 live window 内或已摘要）。
            return {"status": "skip", "reason": "below_window", "total": len(session_msgs)}
        trigger = max(1, int(getattr(settings, "llm_rolling_summary_trigger_messages", 20) or 20))
        if len(candidates) < trigger:
            return {"status": "skip", "reason": "below_trigger", "pending": len(candidates)}

        new_summary = _summarize(candidates, prev_summary)
        if not new_summary:
            return {"status": "skip", "reason": "empty_summary"}

        new_upto = int(candidates[-1]["id"])
        update_session_rolling_summary(
            session_id=session_id,
            rolling_summary=new_summary,
            rolling_summary_upto_id=new_upto,
        )
        return {"status": "updated", "summarized": len(candidates), "upto_id": new_upto}
    except Exception:
        logger.warning(
            "rolling summary update failed account=%s session=%s",
            account_id,
            session_id,
            exc_info=True,
        )
        return {"status": "error"}
