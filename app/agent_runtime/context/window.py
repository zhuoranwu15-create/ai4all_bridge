"""短期对话历史的 token 预算裁剪与单消息硬上限（纯函数、无 I/O，可独立单测）。

裁剪策略对标 OpenClaw agent-core：保留尾部最近消息，从最旧端按 token 预算丢弃整条；
单条超长先截断再计预算。token 估算用粗近似（``len/1.5``），仅用于预算裁剪，**不用于计费**
（真值走 LLM usage）。本模块只负责"喂给 LLM 的历史副本"的形状，不触达 DB、不改落库内容。

设计与取舍见 docs/tech_design/context_window_token_budget_design.md。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from app.time_utils import parse_db_timestamp


# 与 prompt_builder._estimate_tokens 同口径：中文约 1 token/字、混排约 1 token/1.5 字，
# 取 len/1.5 的 ceil 作保守近似。两处口径必须一致，避免预算估算漂移。
def estimate_tokens(text: str) -> int:
    """历史裁剪用的粗略 token 估算（非计费）。"""
    return math.ceil(len(text or "") / 1.5)


# 截断标记复用 prompt_builder._truncate 的风格，保持全代码一致。
_TRUNCATE_MARKER = "...[已截断]"

# rolling_summary（【更早对话摘要】）产物的字符上限：**单一真相源**——摘要生成端
# （context_summarizer）与注入端（prompt_builder）共用此值，避免两处各写 1000 而漂移。
ROLLING_SUMMARY_MAX_CHARS = 1000


def compute_floor_count(
    rows: List[Dict[str, Any]],
    *,
    now: datetime,
    floor_minutes: int,
    floor_turns: int,
) -> int:
    """算出「最近硬底」应保留的尾部条数：距 now ≤ floor_minutes 且 ≤ floor_turns 轮的原文。

    rows 为正序（最旧→最新）。从最新往旧走，取两条件的交集（连续尾块）：一旦某条早于时间窗、
    或再纳入就会超过轮数上限（一个 user 消息计一轮），即停止。至少返回 1（当前消息永远保留）。
    floor_minutes/floor_turns ≤ 0 表示该维度不设限。now 可为 aware/naive，统一按 naive 比较。
    见 docs/tech_design/context_orchestration_unified_design.md §5.1。
    """
    if not rows:
        return 1
    # 两维都关 = 不设硬底（只保底当前消息 1 条）；否则按启用维度取交集。
    if floor_minutes <= 0 and floor_turns <= 0:
        return 1
    now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
    cutoff = now_naive - timedelta(minutes=floor_minutes) if floor_minutes > 0 else None
    count = 0
    user_turns = 0
    for row in reversed(rows):
        if cutoff is not None:
            ts = parse_db_timestamp(row.get("created_at"))
            # 解析成功且早于时间窗 → 停；解析失败不据此中断，交给轮数闸兜底。
            if ts is not None and ts < cutoff:
                break
        if row.get("role") == "user":
            if floor_turns > 0 and user_turns >= floor_turns:
                break
            user_turns += 1
        count += 1
    return max(count, 1)


def cap_message_chars(content: str, max_chars: int) -> Tuple[str, bool]:
    """单条消息字符硬上限：超长则截断到 *max_chars* 并追加标记。

    返回 ``(新内容, 是否发生截断)``。``max_chars <= 0`` 表示不限制。
    """
    if max_chars <= 0 or content is None:
        return content, False
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars] + _TRUNCATE_MARKER, True


def trim_history_rows(
    rows: List[Dict[str, Any]],
    *,
    token_budget: int,
    per_message_max_chars: int,
    min_keep: int = 1,
) -> Dict[str, Any]:
    """对按时间正序（最旧→最新）的历史行做单条截断 + token 预算裁剪。

    入参 *rows*：session tail 取数的返回（已正序、已按 account/session 隔离）。
    返回 ``{"kept", "dropped", "metrics"}``：

    - ``per_message_max_chars > 0``：逐条把超长 ``content`` 截断（拷贝行对象，**不修改入参**，
      因为调用方仍可能基于原始行读取 ``session_id`` 等字段）。
    - ``token_budget > 0``：从最旧端整条丢弃，直到累计 est_token ≤ 预算；**至少保留最后
      ``min_keep`` 条**（尾部最近消息；与 OpenClaw ``capArrayByJsonBytes`` 尾部保留语义一致）。
    - ``min_keep``：最近「硬底」条数——这些尾部消息永不被预算丢弃，**硬底优先于 token 预算**
      （kept 可能因此短暂超预算）。调用方按「15 分钟内、最多 10 轮」等口径先算出 F 的条数传入。
      因为 trim 只从最旧端丢、保留的恒为尾部连续块，「保留最近 F 条」== ``min_keep=len(F)``。
    - 两者 ≤ 0 时各自关闭；``token_budget <= 0`` 不丢弃任何行（零行为变更）。

    裁剪只作用于内容形状，行的其它字段（id/session_id/role/message_id/created_at）原样透传。
    """
    # 1) 单条截断：拷贝行（浅拷贝 + 覆盖 content），避免改动调用方持有的原始 rows。
    capped: List[Dict[str, Any]] = []
    truncated_count = 0
    for row in rows:
        content = row.get("content") or ""
        new_content, was_truncated = cap_message_chars(content, per_message_max_chars)
        if was_truncated:
            truncated_count += 1
            row = {**row, "content": new_content}
        capped.append(row)

    # 2) token 预算：从最旧端（列表头）整条丢弃，保留尾部最近消息。
    #    丢弃循环里的 total 收敛后即为 kept 的累计 token，直接复用，避免对 kept 再扫一遍。
    dropped: List[Dict[str, Any]] = []
    kept = capped
    # 硬底：至少保留最近 min_keep 条（收敛到 [1, len]），预算再紧也不丢。
    _min_keep = max(1, min(int(min_keep or 1), len(capped))) if capped else 1
    if token_budget > 0 and capped:
        total = sum(estimate_tokens(r.get("content")) for r in capped)
        start = 0
        while total > token_budget and start < len(capped) - _min_keep:
            total -= estimate_tokens(capped[start].get("content"))
            start += 1
        if start > 0:
            dropped = capped[:start]
            kept = capped[start:]
        kept_est_tokens = total
    else:
        kept_est_tokens = sum(estimate_tokens(r.get("content")) for r in capped)

    metrics = {
        "input_count": len(rows),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "truncated_count": truncated_count,
        "kept_est_tokens": kept_est_tokens,
    }
    return {"kept": kept, "dropped": dropped, "metrics": metrics}
