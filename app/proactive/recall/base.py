"""召回层（recall/）的统一契约 `Recaller`。

把"生成主动消息候选"统一抽象为**召回**：给定上下文，产出 0..N 个 `ProactiveCandidate`。
**薄协议、不上胖基类** —— 各召回器内核差异是真实的（topic_followup/account_check 走
`generate_completion`，content_invitation 走 `generate_reply_with_tools` 带工具并落
content_invitations 行），强抽模板方法只会逼出假共性。这里只约定对外形状。

scope 维度（阶段2 引入、全局留 stub）：
- ``account``：用单个用户自己的上下文召回（当前 topic_followup / content_invitation）。
- ``global``：生成一次、扇出给所有用户（如近日热点话题），再按每用户历史去重/过滤。
  真正的全局召回需要跨账号候选池（阶段3 的 `proactive_candidates` 表），本轮不实现。

阶段2 现状：自动链路仍由 `plan_reactivation_candidate` 经函数注入调用既有 `generate_*`
（保持行为等价与既有 monkeypatch 测试），候选随后被适配成 `ProactiveCandidate` 进入
selection 层。把现役 `generate_*` 全面改造成 `Recaller` 实例留待阶段3（与池表一起落地）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

try:  # py3.8+ Literal
    from typing import Literal
    RecallScope = Literal["account", "global"]
except ImportError:  # pragma: no cover
    RecallScope = str  # type: ignore

from app.proactive.contract.candidate import ProactiveCandidate


@dataclass(frozen=True)
class RecallContext:
    """召回输入。account_id 为空表示全局召回（不绑定具体用户）。"""

    now: datetime
    account_id: Optional[str] = None


@runtime_checkable
class Recaller(Protocol):
    """一种主动消息召回器的对外契约。

    实现需声明 `kind`（候选种类，与 `selection.ranker` 登记一致）与 `scope`
    （account / global），并实现 `propose` 返回候选列表（可空）。
    """

    kind: str
    scope: "RecallScope"

    def propose(self, ctx: RecallContext) -> List[ProactiveCandidate]:
        ...
