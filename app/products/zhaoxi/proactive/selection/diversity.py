"""选择层：纯排序 + 多样性打散（零 I/O，可单测，未来多召回类型复用）。

`rank_and_diversify`：对已带分数的候选按分排序取 top_k，再把"主题命中最近已推过"的候选
降权后置——避免连续几次推相近主题。纯函数：近期已推主题由调用方（读账号发送历史后）传入。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence


def _norm(value: Any) -> str:
    """与 store.candidates._normalize_dedupe_key 同口径：小写 + 折叠空白。"""
    return " ".join(str(value or "").lower().split())


def rank_and_diversify(
    scored_items: Sequence[Dict[str, Any]],
    recent_topics: Sequence[str],
    *,
    top_k: int = 3,
    penalty: float = 0.5,
    topic_key: str = "topic",
    score_key: str = "score",
) -> List[Dict[str, Any]]:
    """按分排序取 top_k，对 topic 命中 recent_topics 的项乘 penalty 降权后再排序后置。

    - scored_items：每项含 `score_key`(float) 与 `topic_key`(str)。
    - recent_topics：最近已推过的主题（归一化后精确匹配）。
    - 返回长度 <= top_k 的**新**列表（不改入参），已按最终有效分降序；调用方取 [0] 即 top1。

    先取相关性 top_k、再在其中做多样性后置，精确对应"默认留前3条 → 多样性打散 → top1"。
    """
    recent = {_norm(topic) for topic in recent_topics if _norm(topic)}
    ranked = sorted(
        scored_items,
        key=lambda item: float(item.get(score_key) or 0.0),
        reverse=True,
    )[: max(int(top_k), 0)]

    def _effective_score(item: Dict[str, Any]) -> float:
        base = float(item.get(score_key) or 0.0)
        if _norm(item.get(topic_key)) in recent:
            return base * penalty
        return base

    diversified = sorted(ranked, key=_effective_score, reverse=True)
    return [dict(item) for item in diversified]
