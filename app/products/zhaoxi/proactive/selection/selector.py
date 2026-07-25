"""候选选择器（纯机制）。

`select_first`：按给定 rank 顺序依次让各 proposer 产出候选，**首个产出者胜出并短路**
（后续 proposer 不再执行）—— 精确复刻 `plan_reactivation_candidate` 原有"topic 命中就不
再跑 content"的行为。

本模块**零 proactive 依赖**（只认 callable 与字符串 kind + 同包的 `ProactiveCandidate`），
因此不可能与 reactivation/planning 成环。proposer 内部具体怎么调生成器、如何把生成结果适配
成候选，由调用方（reactivation 的 plan 适配器）注入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from app.products.zhaoxi.proactive.contract.candidate import ProactiveCandidate


@dataclass(frozen=True)
class Proposal:
    """一次 proposer 调用的结果。

    `result`：生成器原始返回 dict（原样放进 planning 的返回结构，供观测/审计）。
    `candidate`：从中提取的候选；None 表示该种类本轮未产出可用候选。
    """

    result: Dict[str, Any]
    candidate: Optional[ProactiveCandidate] = None


@dataclass(frozen=True)
class SelectionResult:
    """选择结果。

    `outcomes` 只含**实际执行过**的 proposer（短路后未执行的种类不在其中），
    调用方据此区分"跑了但没产出"与"被抢占未跑"。
    """

    chosen_kind: Optional[str]
    candidate: Optional[ProactiveCandidate]
    outcomes: Dict[str, Proposal]


def select_first(
    ranked_kinds: Sequence[str],
    proposers: Mapping[str, Callable[[], Proposal]],
) -> SelectionResult:
    """按 rank 顺序选出首个产出候选的种类（短路）。"""
    outcomes: Dict[str, Proposal] = {}
    for kind in ranked_kinds:
        proposer = proposers.get(kind)
        if proposer is None:
            continue
        proposal = proposer()
        outcomes[kind] = proposal
        if proposal.candidate is not None:
            return SelectionResult(chosen_kind=kind, candidate=proposal.candidate, outcomes=outcomes)
    return SelectionResult(chosen_kind=None, candidate=None, outcomes=outcomes)
