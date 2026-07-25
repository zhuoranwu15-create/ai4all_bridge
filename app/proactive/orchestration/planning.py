"""主动消息每小时 per-account planning 编排器（从 state.py 抽出）。

scan_due_proactive_account_checks：扫到期账号 → claim → 发到期 companion 候选 +
刷新 reactivation 候选（已有 pending 则不覆盖）。零行为变更迁移；旧路径
`from app.proactive.store.account_state import scan_due_proactive_account_checks` 仍可用（state 惰性再导出）。
"""
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.platform.channels import CHANNEL_WEIXIN
from app.config import settings
from app.db import (
    get_account,
    get_account_last_inbound_at,
    get_account_onboarding_state,
    upsert_proactive_account_state,
)
from app.onboarding import is_onboarding_done
from app.products.zhaoxi.application import (
    HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
    get_human_proactive_last_inbound_at,
    human_level_proactive_allowed,
    resolve_human_proactive_scope,
)
from app.time_utils import beijing_naive_now, parse_db_timestamp
from app.proactive.contract.common import format_reactivation_time
from app.proactive.contract.candidate import ProactiveCandidate
from app.proactive.selection.ranker import ranked_kinds
from app.proactive.selection.selector import Proposal, select_first
from app.proactive.delivery.account_check import (
    decide_account_check_action,
    execute_account_check_decision,
)
from app.proactive.recall.content_invitation import generate_content_invitation_candidate
from app.proactive.recall.hot_topic import select_hot_topic_candidate
from app.proactive.recall.topic_followup import generate_topic_followup_candidate
from app.proactive.slots import _account_allowed_windows, _with_default_schedule
from app.proactive.store.candidates import (
    REACTIVATION_TYPE_CONTENT_INVITATION,
    REACTIVATION_TYPE_HOT_TOPIC,
    REACTIVATION_TYPE_TOPIC_FOLLOWUP,
    clear_reactivation_candidate,
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

NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY = "new_user_reactivation_last_triggered_at"


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


def _int_setting(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _candidate_with_schedule_and_metadata(
    candidate: ProactiveCandidate,
    *,
    now: datetime,
    schedule_from: Optional[datetime],
    allowed_windows: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    legacy = candidate.to_legacy()
    if metadata:
        legacy["metadata"] = {
            **(legacy.get("metadata") if isinstance(legacy.get("metadata"), dict) else {}),
            **metadata,
        }
    return _with_default_schedule(
        legacy,
        now=schedule_from or now,
        allowed_windows=allowed_windows,
    )


def _new_user_reactivation_eligibility(
    *,
    account_id: str,
    now: datetime,
) -> Dict[str, Any]:
    """Return whether this account should get a first-24h idle nudge plan."""
    account = get_account(account_id=account_id)
    if account is None:
        return {"eligible": False, "reason": "account_not_found"}

    onboarding_state = get_account_onboarding_state(account_id=account_id)
    if not is_onboarding_done(onboarding_state):
        return {
            "eligible": False,
            "reason": "onboarding_active",
            "onboarding_state": onboarding_state,
        }

    human_scope = resolve_human_proactive_scope(account_id)
    created_at = parse_db_timestamp(
        human_scope.owner_created_at if human_scope else account.get("created_at")
    )
    # form-A 维持微信口径；world 按真人聚合 owner bindings + 全 resident 的跨渠道入站。
    owner_last_inbound = get_human_proactive_last_inbound_at(account_id)
    last_inbound_at = parse_db_timestamp(
        owner_last_inbound
        if human_scope is not None
        else get_account_last_inbound_at(
            account_id=account_id, channel=CHANNEL_WEIXIN
        )
    )
    if created_at is None:
        return {"eligible": False, "reason": "account_created_at_missing"}
    if last_inbound_at is None:
        return {"eligible": False, "reason": "last_inbound_missing"}

    window_hours = max(1, _int_setting("new_user_reactivation_window_hours", 24))
    idle_hours = max(1, _int_setting("new_user_reactivation_idle_hours", 2))
    cooldown_hours = max(1, _int_setting("new_user_reactivation_cooldown_hours", 6))
    window_end = created_at + timedelta(hours=window_hours)
    if now > window_end:
        return {
            "eligible": False,
            "reason": "new_user_window_elapsed",
            "window_end": format_reactivation_time(window_end),
        }

    earliest_send_at = last_inbound_at + timedelta(hours=idle_hours)
    if now < earliest_send_at:
        return {
            "eligible": False,
            "reason": "idle_threshold_not_met",
            "earliest_send_at": format_reactivation_time(earliest_send_at),
        }

    from app.proactive.store.account_state import get_account_state

    state = get_account_state(account_id=account_id)
    metadata = (state or {}).get("metadata") or {}
    last_triggered_at = parse_db_timestamp(
        metadata.get(NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY)
    )
    next_allowed_at = (
        last_triggered_at + timedelta(hours=cooldown_hours)
        if last_triggered_at is not None
        else None
    )
    if next_allowed_at is not None and now < next_allowed_at:
        return {
            "eligible": False,
            "reason": "new_user_reactivation_cooldown",
            "next_allowed_at": format_reactivation_time(next_allowed_at),
        }

    return {
        "eligible": True,
        "reason": "eligible",
        "created_at": format_reactivation_time(created_at),
        "window_end": format_reactivation_time(window_end),
        "last_inbound_at": format_reactivation_time(last_inbound_at),
        "earliest_send_at": format_reactivation_time(earliest_send_at),
        "idle_hours": round((now - last_inbound_at).total_seconds() / 3600.0, 3),
        "cooldown_hours": cooldown_hours,
    }


def plan_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    topic_followup_generator: Optional[ReactivationGenerator] = None,
    content_invitation_generator: Optional[ReactivationGenerator] = None,
    hot_topic_generator: Optional[ReactivationGenerator] = None,
    schedule_from: Optional[datetime] = None,
    candidate_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Refresh the unified reactivation candidate for one account.

    单账号"召回 → 选择 → 落库"的规划：把各种类包成 proposer，交给 selection.select_first
    按 ranker 顺序短路选出胜者并落库。返回 dict 与历史逐键一致。（阶段2.5 从 delivery/dispatch
    归位到 orchestration，plan 是规划不是派发。）
    """
    current = now or beijing_naive_now()
    if not human_level_proactive_allowed(account_id):
        return _no_op(
            account_id=account_id,
            reason=HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
            now=current,
        )
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

    def _hot_topic_proposer() -> Proposal:
        # 与 topic proposer 同构：跑（注入的）每账号热点选择器，产出 reactivation_candidate 则
        # 适配成 ProactiveCandidate。未注入时兜底 no_op（reason 与 topic 分支同一范式）。
        result = (
            hot_topic_generator(account_id=account_id, now=current)
            if hot_topic_generator
            else _no_op(
                account_id=account_id,
                reason="hot_topic_generator_not_implemented",
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

    selection = select_first(
        ranked_kinds(),
        {
            REACTIVATION_TYPE_TOPIC_FOLLOWUP: _topic_proposer,
            REACTIVATION_TYPE_CONTENT_INVITATION: _content_proposer,
            REACTIVATION_TYPE_HOT_TOPIC: _hot_topic_proposer,
        },
    )
    # topic 是优先级最高的 proposer，必然被执行（select_first 从它开始）。
    topic_result = selection.outcomes[REACTIVATION_TYPE_TOPIC_FOLLOWUP].result

    if selection.chosen_kind is not None:
        candidate = _candidate_with_schedule_and_metadata(
            selection.candidate,
            now=current,
            schedule_from=schedule_from,
            allowed_windows=allowed_windows,
            metadata=candidate_metadata,
        )
        state = upsert_reactivation_candidate(
            account_id=account_id,
            candidate=candidate,
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
        # hot_topic 是末位 proposer：被上游抢占未执行时合成 no_op（reason 记明是谁抢占）。
        hot_topic_outcome = selection.outcomes.get(REACTIVATION_TYPE_HOT_TOPIC)
        hot_topic_generation = (
            hot_topic_outcome.result
            if hot_topic_outcome is not None
            else _no_op(
                account_id=account_id,
                reason=f"{selection.chosen_kind}_candidate_selected",
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
            "hot_topic_generation": hot_topic_generation,
            "evaluated_at": format_reactivation_time(current),
        }

    # 各种类都未产出候选 → 都已执行；reason 回退链沿用 content→topic 原顺序，再补 hot_topic 尾项
    # （既有两类为空的场景 reason 与原实现逐字一致，hot_topic 仅在前两者 reason 皆空时才兜底）。
    content_result = selection.outcomes[REACTIVATION_TYPE_CONTENT_INVITATION].result
    hot_topic_result = selection.outcomes[REACTIVATION_TYPE_HOT_TOPIC].result
    return {
        "action": "no_op",
        "account_id": account_id,
        "reason": content_result.get("reason")
        or topic_result.get("reason")
        or hot_topic_result.get("reason")
        or "no_reactivation_candidate",
        "topic_followup_generation": topic_result,
        "content_invitation_generation": content_result,
        "hot_topic_generation": hot_topic_result,
        "evaluated_at": format_reactivation_time(current),
        "metadata": {},
    }


def plan_new_user_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    topic_followup_generator: Optional[ReactivationGenerator] = None,
    hot_topic_generator: Optional[ReactivationGenerator] = None,
) -> Dict[str, Any]:
    """Plan the first-24h idle nudge, using topic_followup first and hot_topic fallback."""
    current = now or beijing_naive_now()
    if not human_level_proactive_allowed(account_id):
        return _no_op(
            account_id=account_id,
            reason=HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
            now=current,
        )
    eligibility = _new_user_reactivation_eligibility(
        account_id=account_id,
        now=current,
    )
    if not eligibility.get("eligible"):
        reason = str(eligibility.get("reason") or "not_eligible")
        if reason in {"new_user_window_elapsed", "account_created_at_missing"}:
            return _no_op(
                account_id=account_id,
                reason="new_user_reactivation_not_applicable",
                now=current,
                metadata={"eligibility": eligibility},
            )
        return _no_op(
            account_id=account_id,
            reason=reason,
            now=current,
            metadata={"eligibility": eligibility},
        )

    earliest_send_at = parse_db_timestamp(eligibility.get("earliest_send_at")) or current
    schedule_from = max(current, earliest_send_at)
    metadata = {
        "new_user_reactivation": True,
        "new_user_reactivation_version": 1,
        "new_user_reactivation_eligibility": eligibility,
    }
    result = plan_reactivation_candidate(
        account_id=account_id,
        now=current,
        topic_followup_generator=topic_followup_generator,
        content_invitation_generator=None,
        hot_topic_generator=hot_topic_generator,
        schedule_from=schedule_from,
        candidate_metadata=metadata,
    )
    if result.get("action") != "reactivation_candidate_planned":
        result["new_user_reactivation"] = eligibility
        return result

    candidate = result.get("reactivation_candidate") or {}
    scheduled_at = parse_db_timestamp(candidate.get("scheduled_at"))
    window_end = parse_db_timestamp(eligibility.get("window_end"))
    if scheduled_at is not None and window_end is not None and scheduled_at > window_end:
        clear_reactivation_candidate(
            account_id=account_id,
            reason="new_user_reactivation_no_slot_before_window_end",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="new_user_reactivation_no_slot_before_window_end",
            now=current,
            metadata={
                "eligibility": eligibility,
                "scheduled_at": candidate.get("scheduled_at"),
            },
        )

    upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={
            NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY: format_reactivation_time(current)
        },
    )
    result["new_user_reactivation"] = eligibility
    return result


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
            new_user_planning = plan_new_user_reactivation_candidate(
                account_id=claimed["account_id"],
                now=current,
                topic_followup_generator=generate_topic_followup_candidate,
                hot_topic_generator=select_hot_topic_candidate,
            )
            if new_user_planning.get("reason") == "new_user_reactivation_not_applicable":
                reactivation_planning = plan_reactivation_candidate(
                    account_id=claimed["account_id"],
                    now=current,
                    topic_followup_generator=generate_topic_followup_candidate,
                    content_invitation_generator=generate_content_invitation_candidate,
                    hot_topic_generator=select_hot_topic_candidate,
                )
                reactivation_planning["new_user_reactivation"] = (
                    new_user_planning.get("metadata") or {}
                ).get("eligibility")
            else:
                reactivation_planning = new_user_planning
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
