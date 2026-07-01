"""主动消息每小时 per-account planning 编排器（从 state.py 抽出）。

scan_due_proactive_account_checks：扫到期账号 → claim → 发到期 companion 候选 +
刷新 reactivation 候选（已有 pending 则不覆盖）。零行为变更迁移；旧路径
`from app.proactive.store.account_state import scan_due_proactive_account_checks` 仍可用（state 惰性再导出）。
"""
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.time_utils import beijing_naive_now
from app.proactive.contract.common import format_reactivation_time
from app.proactive.contract.candidate import ProactiveCandidate
from app.proactive.selection.ranker import ranked_kinds
from app.proactive.selection.selector import Proposal, select_first
from app.proactive.delivery.account_check import (
    decide_account_check_action,
    execute_account_check_decision,
)
from app.proactive.recall.content_invitation import generate_content_invitation_candidate
from app.proactive.recall.topic_followup import generate_topic_followup_candidate
from app.proactive.slots import _account_allowed_windows, _with_default_schedule
from app.proactive.store.candidates import (
    REACTIVATION_TYPE_CONTENT_INVITATION,
    REACTIVATION_TYPE_TOPIC_FOLLOWUP,
    get_reactivation_candidate,
    get_reactivation_candidate_from_metadata,
    normalize_reactivation_candidate,
    upsert_reactivation_candidate,
)
from app.proactive.store.account_state import (
    DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    claim_due_account_check,
    format_state_time,
    list_due_proactive_account_checks,
    mark_account_check_sent,
)


ReactivationGenerator = Callable[..., Dict[str, Any]]


def _no_op(
    *,
    account_id: str,
    reason: str,
    now: datetime,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "action": "no_op",
        "account_id": account_id,
        "reason": reason,
        "evaluated_at": format_reactivation_time(now),
        "metadata": metadata or {},
    }


def _candidate_from_content_invitation(
    *,
    invitation: Dict[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    invitation_id = str(invitation.get("id") or "").strip()
    return normalize_reactivation_candidate(
        {
            "id": f"reactivation-content-{invitation_id}",
            "type": REACTIVATION_TYPE_CONTENT_INVITATION,
            "content_invitation_id": invitation_id,
            "topic": invitation.get("topic"),
            "text": invitation.get("invitation_text"),
            "generated_at": format_reactivation_time(now),
            "metadata": {
                "title_count": len(invitation.get("title_items") or []),
                "source": "content_invitation_generation",
            },
        }
    )


def plan_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    topic_followup_generator: Optional[ReactivationGenerator] = None,
    content_invitation_generator: Optional[ReactivationGenerator] = None,
) -> Dict[str, Any]:
    """Refresh the unified reactivation candidate for one account.

    单账号"召回 → 选择 → 落库"的规划：把各种类包成 proposer，交给 selection.select_first
    按 ranker 顺序短路选出胜者并落库。返回 dict 与历史逐键一致。（阶段2.5 从 delivery/dispatch
    归位到 orchestration，plan 是规划不是派发。）
    """
    current = now or beijing_naive_now()
    allowed_windows = _account_allowed_windows(account_id)

    # 每个种类一个 proposer：跑生成器 → 把产出适配成 ProactiveCandidate（无产出则 None）。
    # None-generator 的兜底 reason 与原实现逐一对齐。select_first 会按 ranker 顺序短路，
    # 故 topic 命中时 content proposer 不执行（与原"topic 抢占即不跑 content"一致）。
    def _topic_proposer() -> Proposal:
        result = (
            topic_followup_generator(account_id=account_id, now=current)
            if topic_followup_generator
            else _no_op(
                account_id=account_id,
                reason="topic_followup_generator_not_implemented",
                now=current,
            )
        )
        raw = result.get("reactivation_candidate")
        candidate = (
            ProactiveCandidate.from_legacy(raw, account_id=account_id)
            if isinstance(raw, dict)
            else None
        )
        return Proposal(result=result, candidate=candidate)

    def _content_proposer() -> Proposal:
        if content_invitation_generator is None:
            result = _no_op(
                account_id=account_id,
                reason="content_invitation_generator_missing",
                now=current,
            )
        else:
            result = content_invitation_generator(account_id=account_id, now=current)
        invitation = result.get("content_invitation")
        candidate = None
        if result.get("action") == "content_invitation_candidate_created" and isinstance(
            invitation, dict
        ):
            candidate = ProactiveCandidate.from_legacy(
                _candidate_from_content_invitation(invitation=invitation, now=current),
                account_id=account_id,
            )
        return Proposal(result=result, candidate=candidate)

    selection = select_first(
        ranked_kinds(),
        {
            REACTIVATION_TYPE_TOPIC_FOLLOWUP: _topic_proposer,
            REACTIVATION_TYPE_CONTENT_INVITATION: _content_proposer,
        },
    )
    # topic 是优先级最高的 proposer，必然被执行（select_first 从它开始）。
    topic_result = selection.outcomes[REACTIVATION_TYPE_TOPIC_FOLLOWUP].result

    if selection.chosen_kind is not None:
        state = upsert_reactivation_candidate(
            account_id=account_id,
            candidate=_with_default_schedule(
                selection.candidate.to_legacy(), now=current, allowed_windows=allowed_windows
            ),
        )
        content_outcome = selection.outcomes.get(REACTIVATION_TYPE_CONTENT_INVITATION)
        # content 被 topic 抢占未执行时，沿用原实现的合成 no_op。
        content_generation = (
            content_outcome.result
            if content_outcome is not None
            else _no_op(
                account_id=account_id,
                reason="topic_followup_candidate_selected",
                now=current,
            )
        )
        return {
            "action": "reactivation_candidate_planned",
            "account_id": account_id,
            "reactivation_type": selection.chosen_kind,
            "reactivation_candidate": get_reactivation_candidate_from_metadata(
                state.get("metadata") or {}
            ),
            "topic_followup_generation": topic_result,
            "content_invitation_generation": content_generation,
            "evaluated_at": format_reactivation_time(current),
        }

    # 两个种类都未产出候选 → 都已执行；reason 回退链与原实现一致。
    content_result = selection.outcomes[REACTIVATION_TYPE_CONTENT_INVITATION].result
    return {
        "action": "no_op",
        "account_id": account_id,
        "reason": content_result.get("reason") or topic_result.get("reason") or "no_reactivation_candidate",
        "topic_followup_generation": topic_result,
        "content_invitation_generation": content_result,
        "evaluated_at": format_reactivation_time(current),
        "metadata": {},
    }


def scan_due_proactive_account_checks(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Per-account planning pass (companion followup + reactivation candidate).

    This NO LONGER sends reactivation messages. Sending is a separate, every-tick
    sweep (`dispatch_due_reactivation_candidates`) keyed off the candidate's slot
    time, so a candidate fires at its slot rather than at this hourly pass. Here
    we only (a) send a due companion-followup candidate, and (b) refresh the
    reactivation candidate — but never overwrite one that is already queued.
    node_id 非空时只处理归属该节点的账号（厚节点改造 P4 调度分片）。

    注意：这里的 (a) 依赖 `account_check_candidate` 已存在，而该候选**只由 admin 端点**人工
    promote 产生（见 recall.manual_companion 模块说明）；本编排器不会自动生成它，故无人工候选时
    `decide_account_check_action` 恒返回 no_op，account_check 这条仅作为 admin 工具存在。
    """
    current = now or beijing_naive_now()
    due_accounts = list_due_proactive_account_checks(now=current, limit=limit, node_id=node_id)
    results: List[Dict[str, Any]] = []
    for item in due_accounts:
        claimed = claim_due_account_check(
            account_id=item["account_id"],
            now=current,
            interval_seconds=planning_interval_seconds,
        )
        if claimed is None:
            results.append(
                {
                    "status": "skipped",
                    "reason": "not_due_or_already_claimed",
                    "account_id": item["account_id"],
                }
            )
            continue
        decision = decide_account_check_action(
            account_id=claimed["account_id"],
            now=current,
        )
        execution = execute_account_check_decision(
            decision=decision,
            now=current,
        )
        account_state = claimed
        if execution.get("status") == "sent":
            account_state = mark_account_check_sent(
                account_id=claimed["account_id"],
                now=current,
            )
            reactivation_planning = {
                "action": "no_op",
                "account_id": claimed["account_id"],
                "reason": "companion_followup_sent_this_check",
                "evaluated_at": format_state_time(current),
                "metadata": {},
            }
            content_invitation_generation = reactivation_planning
        elif get_reactivation_candidate(account_id=claimed["account_id"]) is not None:
            # A candidate is already queued (waiting for its slot, or due and
            # awaiting the dispatch sweep). Do NOT replan/overwrite it; the slot
            # firing and cancel-on-new-inbound both happen in the dispatch path.
            # (Cancelling there clears the candidate, so the NEXT hourly pass
            # regenerates a fresh one.)
            reactivation_planning = {
                "action": "no_op",
                "account_id": claimed["account_id"],
                "reason": "reactivation_candidate_pending",
                "evaluated_at": format_state_time(current),
                "metadata": {},
            }
            content_invitation_generation = reactivation_planning
        else:
            reactivation_planning = plan_reactivation_candidate(
                account_id=claimed["account_id"],
                now=current,
                topic_followup_generator=generate_topic_followup_candidate,
                content_invitation_generator=generate_content_invitation_candidate,
            )
            content_invitation_generation = reactivation_planning.get(
                "content_invitation_generation",
                reactivation_planning,
            )
        results.append(
            {
                "status": execution["status"],
                "reason": execution.get("reason"),
                "account_id": claimed["account_id"],
                "decision": decision,
                "execution": execution,
                "reactivation_planning": reactivation_planning,
                "content_invitation_generation": content_invitation_generation,
                "account_state": account_state,
            }
        )
    return results
