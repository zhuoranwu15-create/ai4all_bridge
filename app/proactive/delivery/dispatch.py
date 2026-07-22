"""自主外联（拉活）候选的**派发层**：取到期候选 → policy → 发送 → 状态机。

`dispatch_reactivation_candidate` / `dispatch_due_reactivation_candidates`：到期候选经
`delivery.outbound.dispatch_proactive_text`（内含 policy 闸门）发送，并推进 content_invitation
行与候选的状态机（含阶段0 的 cancelled 改期/清除）。

规划选择（召回→选择→落库）已归位 orchestration/planning（plan 是规划不是派发）。
存储读写委托 store.candidates，调度时间委托 slots。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.channels import CHANNEL_APP
from app.config import settings
from app.platform import (
    HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
    count_human_proactive_inbound_after,
    human_level_proactive_allowed,
    resolve_human_proactive_scope,
)
from app.db import (
    claim_content_invitation_for_send,
    list_due_reactivation_candidate_accounts,
    mark_content_invitation_invited,
    release_content_invitation_claim,
)
from app.proactive.contract.common import _select_route
from app.proactive.delivery.outbound import dispatch_proactive_text
from app.proactive.delivery.touch_state import STALE, get_account_touch_state
from app.time_utils import beijing_naive_now
from app.proactive.slots import _account_allowed_windows, _next_slot_after
from app.proactive.contract.common import format_reactivation_time
from app.proactive.store.candidates import (
    REACTIVATION_TYPE_CONTENT_INVITATION,
    REACTIVATION_TYPE_HOT_TOPIC,
    _avoidance_count,
    _clean_text,
    _has_sent_reactivation_today,
    _inbound_count_after,
    _parse_reactivation_time,
    _reactivation_product_category,
    clear_reactivation_candidate,
    get_reactivation_candidate,
    get_reactivation_candidate_from_metadata,
    reactivation_outbound_metadata,
    reschedule_reactivation_candidate,
    rule_reactivation_dedupe_check,
)


DedupeChecker = Callable[..., Dict[str, Any]]


def _is_new_user_reactivation(candidate: Dict[str, Any]) -> bool:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    return bool(metadata.get("new_user_reactivation"))


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


def dispatch_reactivation_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
    dry_run: bool = True,
    dedupe_checker: Optional[DedupeChecker] = None,
) -> Dict[str, Any]:
    """Revalidate and optionally send the current reactivation candidate.

    In dry_run mode this never creates outbound rows and never calls OpenClaw.

    新消息处理：若候选生成后用户又有入站消息（说明用户已自行回来/在线），直接取消本次
    推送，由下一次 planning 生成更新鲜的候选——取代旧的「最近 N 分钟改期」逻辑。
    去重改为生成时语义去重 + 发送时零成本精确兜底（`rule_reactivation_dedupe_check`），
    发送公共路径不再有 LLM 调用。
    """
    current = now or beijing_naive_now()
    if not human_level_proactive_allowed(account_id):
        clear_reactivation_candidate(
            account_id=account_id,
            reason=HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason=HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
            now=current,
        )
    allowed_windows = _account_allowed_windows(account_id)
    candidate = get_reactivation_candidate(account_id=account_id)
    if candidate is None:
        return _no_op(account_id=account_id, reason="reactivation_candidate_missing", now=current)

    scheduled_at = _parse_reactivation_time(candidate.get("scheduled_at"))
    if scheduled_at is not None and scheduled_at > current:
        return {
            "action": "not_due",
            "account_id": account_id,
            "reactivation_candidate": candidate,
            "scheduled_at": candidate.get("scheduled_at"),
            "evaluated_at": format_reactivation_time(current),
        }

    is_new_user_reactivation = _is_new_user_reactivation(candidate)
    if (not is_new_user_reactivation) and _has_sent_reactivation_today(
        account_id=account_id,
        now=current,
    ):
        clear_reactivation_candidate(
            account_id=account_id,
            reason="reactivation_daily_limit_already_sent",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="reactivation_daily_limit_already_sent",
            now=current,
        )

    # 候选生成后用户又说过话 → 取消本次推送（用户已自行回来，拉活无意义）；
    # 仅统计入站消息（不含 bot 自身/提醒等出站），以候选 generated_at 为基准。
    generated_at = _parse_reactivation_time(candidate.get("generated_at"))
    if generated_at is not None:
        owner_inbound_since = count_human_proactive_inbound_after(
            account_id, after=format_reactivation_time(generated_at)
        )
        inbound_since = (
            owner_inbound_since
            if owner_inbound_since is not None
            else _inbound_count_after(account_id=account_id, after=generated_at)
        )
        if inbound_since > 0:
            clear_reactivation_candidate(
                account_id=account_id,
                reason="inbound_since_candidate",
                now=current,
            )
            return _no_op(
                account_id=account_id,
                reason="inbound_since_candidate",
                now=current,
                metadata={"inbound_since_candidate_count": inbound_since},
            )

    avoidance_count = _avoidance_count(account_id=account_id, now=current)
    if avoidance_count > 0:
        next_slot = _next_slot_after(candidate, now=current, allowed_windows=allowed_windows)
        if next_slot:
            state = reschedule_reactivation_candidate(
                account_id=account_id,
                candidate=candidate,
                scheduled_slot=next_slot["scheduled_slot"],
                scheduled_at=next_slot["scheduled_at"],
                reason="avoidance_window",
                now=current,
            )
            return {
                "action": "delayed",
                "account_id": account_id,
                "reason": "avoidance_window",
                "avoidance_count": avoidance_count,
                "reactivation_candidate": get_reactivation_candidate_from_metadata(
                    state.get("metadata") or {}
                ),
                "evaluated_at": format_reactivation_time(current),
            }
        clear_reactivation_candidate(
            account_id=account_id,
            reason="avoidance_window_final_slot",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="avoidance_window_final_slot",
            now=current,
            metadata={"avoidance_count": avoidance_count},
        )

    # 零成本精确去重兜底（无 LLM）：命中完全相同的 topic/文案则取消，等下次生成。
    checker = dedupe_checker or rule_reactivation_dedupe_check
    dedupe = checker(account_id=account_id, candidate=candidate, now=current)
    if dedupe.get("duplicate"):
        clear_reactivation_candidate(
            account_id=account_id,
            reason="dedupe_duplicate",
            now=current,
        )
        return _no_op(
            account_id=account_id,
            reason="dedupe_duplicate",
            now=current,
            metadata={"dedupe": dedupe},
        )

    route = _select_route(account_id)
    if route is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    # 微信仍要求 24h context token；App inbox 是原生拉取通道，不套微信可达窗口。
    if (
        route.get("channel") != CHANNEL_APP
        and get_account_touch_state(account_id=account_id, now=current) == STALE
    ):
        clear_reactivation_candidate(
            account_id=account_id,
            reason="proactive_touch_stale",
            now=current,
        )
        return _no_op(account_id=account_id, reason="proactive_touch_stale", now=current)

    outbound_metadata = reactivation_outbound_metadata(
        candidate={**candidate, "dedupe": dedupe},
        sent_at=current,
        extra={
            "source": (
                "new_user_reactivation_scheduler"
                if is_new_user_reactivation
                else "reactivation_scheduler"
            ),
            "policy": {
                "daily_limit_key": (
                    "new_user_reactivation"
                    if is_new_user_reactivation
                    else "reactivation"
                ),
                "avoidance_applied": False,
            },
            "channel_binding_id": route.get("channel_binding_id"),
            "dry_run": bool(dry_run),
        },
    )
    if dry_run:
        return {
            "action": "would_send",
            "account_id": account_id,
            "text": candidate["text"],
            "reactivation_candidate": candidate,
            "outbound_metadata": outbound_metadata,
            "evaluated_at": format_reactivation_time(current),
        }

    # per-type hot_topic dry_run gate：hot_topic_dispatch_dry_run=True（默认）时只记录
    # would_send，其他候选类型不受影响。两个开关都 false 才真实发热点消息。
    if (
        not is_new_user_reactivation
        and candidate.get("type") == REACTIVATION_TYPE_HOT_TOPIC
        and bool(getattr(settings, "hot_topic_dispatch_dry_run", True))
    ):
        return {
            "action": "would_send",
            "account_id": account_id,
            "text": candidate["text"],
            "reactivation_candidate": candidate,
            "outbound_metadata": {**outbound_metadata, "dry_run": True},
            "evaluated_at": format_reactivation_time(current),
        }

    # content_invitation candidates are backed by a content_invitations row that
    # drives the downstream "send titles" interaction. Reactivation is now the
    # single dispatch path, so it owns advancing that row's state machine:
    # candidate -> sending (claim) -> invited (on send) / candidate (on failure).
    is_content_invitation = candidate["type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    invitation_id = (
        _clean_text(candidate.get("content_invitation_id")) if is_content_invitation else ""
    )
    if is_content_invitation:
        if not invitation_id:
            clear_reactivation_candidate(
                account_id=account_id, reason="content_invitation_id_missing", now=current
            )
            return _no_op(
                account_id=account_id, reason="content_invitation_id_missing", now=current
            )
        # Claim ignores scheduled_at; timing was already decided above.
        claimed_invitation = claim_content_invitation_for_send(invitation_id=invitation_id)
        if claimed_invitation is None:
            # Row already sent/expired/claimed elsewhere -> candidate is stale.
            clear_reactivation_candidate(
                account_id=account_id, reason="content_invitation_not_claimable", now=current
            )
            return _no_op(
                account_id=account_id, reason="content_invitation_not_claimable", now=current
            )

    outbound = dispatch_proactive_text(
        account_id=account_id,
        channel=route["channel"],
        channel_account_id=route.get("channel_account_id"),
        to_user_id=route["to_user_id"],
        session_key=route.get("session_key"),
        source="new_user_reactivation" if is_new_user_reactivation else "reactivation",
        text=candidate["text"],
        idempotency_key=f"reactivation-{account_id}-{candidate['id']}-{current.date().isoformat()}",
        now=current,
        product_category=(
            "new_user_reactivation"
            if is_new_user_reactivation
            else _reactivation_product_category(candidate["type"])
        ),
        metadata=outbound_metadata,
    )
    outbound_status = outbound.get("status")
    if is_content_invitation:
        if outbound_status == "sent":
            outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
            mark_content_invitation_invited(
                invitation_id=invitation_id,
                outbound_message_id=outbound_id,
                invited_at=format_reactivation_time(current),
            )
        elif outbound_status not in {"pending", "sending"}:
            # Send blocked/failed -> reset the row so a later slot can retry.
            release_content_invitation_claim(invitation_id=invitation_id)

    if outbound_status == "sent":
        clear_reactivation_candidate(
            account_id=account_id,
            reason="sent",
            now=current,
        )
        action = "sent"
    elif outbound_status in {"pending", "sending"}:
        action = "queued"
    elif outbound_status in {"cancelled", "failed"}:
        # cancelled=策略/moderation 拦截；failed=下游拒收（含 ret:-2 限速）。两者此时
        # scheduled_at 都已是过去：若原样保留候选，下一次扫描仍判定为 due，每个 tick 重复
        # 发送。failed 尤甚——拉活日节流只数 pending/sending/sent（见
        # count_reactivation_outbound_for_quota_date），失败行拦不住兜底闸，持续限速时会
        # 每 tick 真发一次形成发送风暴。因此统一把候选改期到下一个发送 slot；已无可用 slot
        # 时清除，等下次 planning 重新生成。（把每-tick 重试降为每-slot 重试，不改
        # policy/avoidance 口径——见 bug B/failed 分支修复。）
        reschedule_reason = (
            "send_blocked_policy" if outbound_status == "cancelled" else "send_failed_downstream"
        )
        next_slot = _next_slot_after(candidate, now=current, allowed_windows=allowed_windows)
        if next_slot:
            state = reschedule_reactivation_candidate(
                account_id=account_id,
                candidate=candidate,
                scheduled_slot=next_slot["scheduled_slot"],
                scheduled_at=next_slot["scheduled_at"],
                reason=reschedule_reason,
                now=current,
            )
            return {
                "action": "delayed",
                "account_id": account_id,
                "reason": outbound.get("error") or reschedule_reason,
                "outbound_message": outbound,
                "reactivation_candidate": get_reactivation_candidate_from_metadata(
                    state.get("metadata") or {}
                ),
                "evaluated_at": format_reactivation_time(current),
            }
        clear_reactivation_candidate(
            account_id=account_id,
            reason=f"{reschedule_reason}_final_slot",
            now=current,
        )
        return {
            "action": "send_blocked",
            "account_id": account_id,
            "reason": outbound.get("error") or reschedule_reason,
            "outbound_message": outbound,
            "reactivation_candidate": candidate,
            "evaluated_at": format_reactivation_time(current),
        }
    else:
        action = "send_blocked"
    return {
        "action": action,
        "account_id": account_id,
        "reason": outbound.get("error"),
        "outbound_message": outbound,
        "reactivation_candidate": candidate,
        "evaluated_at": format_reactivation_time(current),
    }


def dispatch_due_reactivation_candidates(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    dispatch_enabled: Optional[bool] = None,
    dry_run: Optional[bool] = None,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Send any reactivation candidates whose slot time has arrived.

    Runs every scheduler tick, decoupled from the hourly planning pass, so a
    candidate fires at its scheduled slot (plus jitter) rather than waiting for
    the account's next planning. Gated by reactivation_dispatch_enabled; defaults
    fail closed (disabled + dry-run) unless settings/caller opt in.
    node_id 非空时只处理归属该节点的账号（厚节点改造 P4 调度分片）。
    """
    current = now or beijing_naive_now()
    if dispatch_enabled is None:
        dispatch_enabled = bool(getattr(settings, "reactivation_dispatch_enabled", False))
    if dry_run is None:
        dry_run = bool(getattr(settings, "reactivation_dispatch_dry_run", True))
    if not dispatch_enabled:
        return []
    due_accounts = list_due_reactivation_candidate_accounts(
        now=format_reactivation_time(current),
        limit=max(1, int(limit)) * 11,
        node_id=node_id,
    )
    grouped_accounts: Dict[str, str] = {}
    for account_id in due_accounts:
        scope = resolve_human_proactive_scope(account_id)
        key = scope.platform_user_id if scope else f"account:{account_id}"
        existing = grouped_accounts.get(key)
        if existing is None or (
            human_level_proactive_allowed(account_id)
            and not human_level_proactive_allowed(existing)
        ):
            grouped_accounts[key] = account_id
    results: List[Dict[str, Any]] = []
    for account_id in list(grouped_accounts.values())[: max(1, int(limit))]:
        results.append(
            dispatch_reactivation_candidate(
                account_id=account_id,
                now=current,
                dry_run=dry_run,
            )
        )
    return results
