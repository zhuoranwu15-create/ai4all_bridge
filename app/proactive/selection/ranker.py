"""离散族（自主外联）召回候选的优先级登记。

纯数据层，沿 `categories.py` registry 范式：新增一种自主外联候选 = 在 `DISCRETIONARY_RANK`
里加一行 + 给个 rank，无需改 planning/selector。阶段2 的"平凡 ranker" = 复刻现状的固定
优先级（topic_followup 先于 content_invitation），保证行为等价；阶段4 再换成按 features
（兴趣/回复率/疲劳）打分排序。
"""
from __future__ import annotations

from typing import Tuple


# 越靠前优先级越高；与 plan_reactivation_candidate 原 if/else 顺序一致。
# account_check / manual_companion 是 admin 手工候选、自动链路不生成，不入自动 rank。
# hot_topic（近期热点，全局召回）置末位 fallback：个人续聊/内容邀请命中即短路，二者皆空
# 才用热点兜底。顺序为产品取舍，可按效果调整（改此元组一处即可，planning/selector 不动）。
DISCRETIONARY_RANK: Tuple[str, ...] = (
    "topic_followup",
    "content_invitation",
    "hot_topic",
)


def ranked_kinds() -> Tuple[str, ...]:
    """返回自动召回的候选种类，按优先级从高到低。"""
    return DISCRETIONARY_RANK


def rank_index(kind: str) -> int:
    """候选种类的优先级序号（越小越优先）；未登记的排到最后。"""
    try:
        return DISCRETIONARY_RANK.index(kind)
    except ValueError:
        return len(DISCRETIONARY_RANK)
