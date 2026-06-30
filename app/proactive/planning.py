"""主动消息每小时 per-account planning 编排器（从 state.py 抽出）。

scan_due_proactive_account_checks：扫到期账号 → claim → 发到期 companion 候选 +
刷新 reactivation 候选（已有 pending 则不覆盖）。零行为变更迁移；旧路径
`from app.proactive.state import scan_due_proactive_account_checks` 仍可用（state 惰性再导出）。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.time_utils import beijing_naive_now
from app.proactive.account_checks import (
    decide_account_check_action,
    execute_account_check_decision,
    generate_content_invitation_candidate,
    generate_topic_followup_candidate,
)
from app.proactive.reactivation import (
    get_reactivation_candidate,
    plan_reactivation_candidate,
)
from app.proactive.state import (
    DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    claim_due_account_check,
    format_state_time,
    list_due_proactive_account_checks,
    mark_account_check_sent,
)


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
    promote 产生（见 `account_checks.py` 模块说明）；本编排器不会自动生成它，故无人工候选时
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
