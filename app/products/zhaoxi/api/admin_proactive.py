"""Admin proactive 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.products.zhaoxi.api.admin_proactive.settings"。"""
import asyncio
import functools
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.config import settings
from app.routers.deps import verify_admin_auth
from app.routers.serializers import _beijing_display, _content_invitation_for_overview, _list_reactivation_candidate_admin_items, _normalize_optional_state_datetime, _normalize_ts, _proactive_message_settings_with_resolved, _proactive_state_for_overview, _redact_text_field
from app.db import cancel_proactive_commitment, cleanup_app_notifications_batch, get_account, get_proactive_account_state, get_proactive_commitment, list_content_invitations_for_account, list_outbound_messages, list_proactive_commitments_for_account, list_proactive_message_setting_events, list_reactivation_outbound_messages_admin, list_reminders_for_account, upsert_proactive_account_state
from app.platform.media.reclaim import reclaim_orphan_media_batch
from app.products.zhaoxi.proactive.recall.manual_companion import clear_account_check_candidate_draft, generate_account_check_candidate_draft, promote_account_check_candidate_draft
from app.products.zhaoxi.proactive.delivery.account_check import decide_account_check_action, execute_account_check_decision
from app.products.zhaoxi.proactive.recall.content_invitation import generate_content_invitation_candidate
from app.products.zhaoxi.proactive.recall.hot_topic import refresh_hot_topic_pool, select_hot_topic_candidate
from app.products.zhaoxi.proactive.recall.topic_followup import generate_topic_followup_candidate
from app.products.zhaoxi.proactive.store.candidates import REACTIVATION_TYPES
from app.products.zhaoxi.proactive.delivery.dispatch import dispatch_reactivation_candidate
from app.products.zhaoxi.proactive.orchestration.planning import plan_reactivation_candidate
from app.products.zhaoxi.proactive.orchestration.scheduler import get_proactive_scheduler, run_proactive_scheduler_once
from app.products.zhaoxi.proactive.preferences import get_effective_proactive_message_settings
from app.products.zhaoxi.application.memory.session_lifecycle import run_daily_dreaming_scan
from app.time_utils import beijing_now
from datetime import timedelta
from typing import Any, Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


class ProactiveAccountStateUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    next_scan_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(default=None)


@router.get("/admin/accounts/{account_id}/proactive-overview")
def admin_account_proactive_overview(
    account_id: str,
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    return {
        "account_id": account_id,
        "proactive_state": _proactive_state_for_overview(
            get_proactive_account_state(account_id=account_id)
        ),
        # 主动消息设定：有效视图（policy 实际读到的合并值）+ 最近变更审计。
        "proactive_message_settings": _proactive_message_settings_with_resolved(
            get_effective_proactive_message_settings(account_id)
        ),
        "proactive_message_setting_events": list_proactive_message_setting_events(
            account_id=account_id,
            limit=limit,
        ),
        "reminders": [
            _normalize_ts(_redact_text_field(reminder), "due_at")
            for reminder in list_reminders_for_account(account_id=account_id, limit=limit)
        ],
        "commitments": [
            _normalize_ts(_redact_text_field(_redact_text_field(commitment), field="reason"))
            for commitment in list_proactive_commitments_for_account(
                account_id=account_id,
                limit=limit,
            )
        ],
        "content_invitations": [
            _content_invitation_for_overview(invitation)
            for invitation in list_content_invitations_for_account(
                account_id=account_id,
                limit=limit,
            )
        ],
        "outbound_messages": [
            _normalize_ts(_redact_text_field(message))
            for message in list_outbound_messages(account_id=account_id, limit=limit)
        ],
        "redacted": True,
    }


@router.get("/admin/accounts/{account_id}/proactive-state")
def admin_get_proactive_account_state(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    # Raw accessor (also backs the edit form round-trip); display normalization
    # happens on the read-only overview surfaces, not here.
    return {
        "account_id": account_id,
        "proactive_state": get_proactive_account_state(account_id=account_id),
    }


@router.patch("/admin/accounts/{account_id}/proactive-state")
def admin_update_proactive_account_state(
    account_id: str,
    payload: ProactiveAccountStateUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")

    updates = payload.model_dump(exclude_unset=True)
    if "next_scan_at" in updates:
        updates["next_scan_at"] = _normalize_optional_state_datetime(
            updates["next_scan_at"],
            field_name="next_scan_at",
        )
    if "cooldown_until" in updates:
        updates["cooldown_until"] = _normalize_optional_state_datetime(
            updates["cooldown_until"],
            field_name="cooldown_until",
        )
    state = upsert_proactive_account_state(
        account_id=account_id,
        **updates,
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "proactive_state": state,
    }


@router.post("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_generate_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = generate_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@router.post("/admin/accounts/{account_id}/proactive-check-candidate-draft/promote")
def admin_promote_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = promote_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@router.delete("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_clear_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = clear_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@router.post("/admin/accounts/{account_id}/proactive-check/run-once")
def admin_run_account_proactive_check_once(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    current = beijing_now()
    # Mirror what scan_due_proactive_account_checks does for one account so the
    # admin endpoint surfaces the same dispatch/plan results as the scheduler.
    # Dispatch always runs in dry_run mode here regardless of the production
    # killswitch — operators use this endpoint to QA the candidate.
    reactivation_dispatch = dispatch_reactivation_candidate(
        account_id=account_id,
        now=current,
        dry_run=True,
    )
    dispatch_action = reactivation_dispatch.get("action")
    dispatch_short_circuit_reasons = {
        "reactivation_daily_limit_already_sent",
        "inbound_since_candidate",
        "avoidance_window_final_slot",
        "dedupe_duplicate",
        "missing_channel_route",
    }
    dispatch_handled = dispatch_action in {
        "sent",
        "would_send",
        "delayed",
        "send_blocked",
        "not_due",
    } or (
        dispatch_action == "no_op"
        and reactivation_dispatch.get("reason") in dispatch_short_circuit_reasons
    )

    if dispatch_handled:
        decision = {
            "action": "no_op",
            "account_id": account_id,
            "reason": "reactivation_dispatch_handled",
            "evaluated_at": current.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        }
        execution = {"status": "skipped", "reason": "reactivation_dispatch_handled"}
        reactivation_planning = {
            "action": "no_op",
            "account_id": account_id,
            "reason": "reactivation_dispatch_handled",
            "evaluated_at": current.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
            "metadata": {},
        }
        content_generation = reactivation_planning
    else:
        decision = decide_account_check_action(account_id=account_id, now=current)
        execution = execute_account_check_decision(decision=decision, now=current)
        if execution.get("status") == "sent":
            reactivation_planning = {
                "action": "no_op",
                "account_id": account_id,
                "reason": "companion_followup_sent_this_run",
                "evaluated_at": current.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
                "metadata": {},
            }
            content_generation = reactivation_planning
        else:
            reactivation_planning = plan_reactivation_candidate(
                account_id=account_id,
                now=current,
                topic_followup_generator=generate_topic_followup_candidate,
                content_invitation_generator=generate_content_invitation_candidate,
            )
            content_generation = reactivation_planning.get(
                "content_invitation_generation",
                reactivation_planning,
            )

    invitation = (content_generation or {}).get("content_invitation")
    content_generation_metadata = (content_generation or {}).get("metadata") or {}
    display = {
        "content_invitation_generated": bool(invitation),
        "reason": None if invitation else (content_generation or {}).get("reason"),
        "detail": None if invitation else content_generation_metadata.get("reply"),
        "content_invitation": invitation,
    }
    return {
        "status": "ok",
        "account_id": account_id,
        "account_check": {
            "decision": decision,
            "execution": execution,
        },
        "reactivation_dispatch": reactivation_dispatch,
        "reactivation_planning": reactivation_planning,
        "content_invitation_generation": content_generation,
        "display": display,
    }


@router.get("/admin/accounts/{account_id}/commitments")
def admin_list_account_commitments(
    account_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return {
        "account_id": account_id,
        "commitments": [
            _normalize_ts(c)
            for c in list_proactive_commitments_for_account(
                account_id=account_id,
                status=status,
                limit=limit,
            )
        ],
    }


@router.post("/admin/commitments/{commitment_id}/cancel")
def admin_cancel_commitment(
    commitment_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    commitment = get_proactive_commitment(commitment_id=commitment_id)
    if commitment is None:
        raise HTTPException(status_code=404, detail="commitment not found")
    cancelled = cancel_proactive_commitment(
        commitment_id=commitment_id,
        error="admin_cancelled",
    )
    return {"status": "ok", "commitment": cancelled}


@router.get("/admin/proactive/scheduler")
def admin_proactive_scheduler_status(_: None = Depends(verify_admin_auth)) -> dict:
    scheduler = get_proactive_scheduler()
    return {
        "enabled": bool(getattr(settings, "proactive_scheduler_enabled", False)),
        "configured": {
            "interval_seconds": settings.proactive_scheduler_interval_seconds,
            "batch_size": settings.proactive_scheduler_batch_size,
            "bypass_quiet_hours": settings.proactive_scheduler_bypass_quiet_hours,
            "planning_interval_seconds": settings.proactive_planning_interval_seconds,
        },
        "scheduler": scheduler.status() if scheduler else None,
    }


@router.get("/admin/proactive/reactivation-candidates")
def admin_proactive_reactivation_candidates(
    type: Optional[str] = None,
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    candidate_type = str(type or "").strip()
    if candidate_type and candidate_type not in REACTIVATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail="type must be topic_followup or content_invitation",
        )
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    items = _list_reactivation_candidate_admin_items(
        candidate_type=candidate_type or None,
        limit=limit,
    )
    summary: dict[str, Any] = {
        "total": len(items),
        "by_type": {},
        "by_scheduled_slot": {},
    }
    for item in items:
        candidate = item.get("reactivation_candidate") or {}
        ctype = str(candidate.get("type") or "unknown")
        slot = str(candidate.get("scheduled_slot") or "unscheduled")
        summary["by_type"][ctype] = summary["by_type"].get(ctype, 0) + 1
        summary["by_scheduled_slot"][slot] = summary["by_scheduled_slot"].get(slot, 0) + 1
    return {
        "items": items,
        "summary": summary,
        "generated_at": beijing_now().replace(microsecond=0, tzinfo=None).isoformat(sep=" "),
        "redacted": False,
    }


@router.get("/admin/proactive/reactivation-history")
def admin_proactive_reactivation_history(
    account_id: Optional[str] = None,
    days: int = 14,
    limit: int = 200,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    since_dt = beijing_now() - timedelta(days=days)
    # outbound_messages.created_at is stored as Beijing-local time
    since_str = since_dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    rows = list_reactivation_outbound_messages_admin(
        account_id=account_id or None,
        since=since_str,
        limit=limit,
    )
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "account_id": row["account_id"],
                "account_display_name": row.get("account_display_name"),
                "text": row["text"],
                "status": row["status"],
                "product_category": row.get("product_category"),
                "quota_date": row.get("quota_date"),
                "created_at": _beijing_display(row.get("created_at")),
                "reactivation_type": (row.get("metadata") or {}).get("reactivation_type"),
                "topic": (row.get("metadata") or {}).get("topic"),
                "scheduled_slot": (row.get("metadata") or {}).get("scheduled_slot"),
                "reactivation_candidate_id": (row.get("metadata") or {}).get("reactivation_candidate_id"),
            }
        )
    summary: dict = {"total": len(items), "by_type": {}, "by_date": {}}
    for item in items:
        rtype = item.get("reactivation_type") or "unknown"
        summary["by_type"][rtype] = summary["by_type"].get(rtype, 0) + 1
        date_key = (item.get("quota_date") or "")[:10]
        if date_key:
            summary["by_date"][date_key] = summary["by_date"].get(date_key, 0) + 1
    return {
        "items": items,
        "summary": summary,
        "filters": {"account_id": account_id, "days": days},
        "generated_at": beijing_now().replace(microsecond=0, tzinfo=None).isoformat(sep=" "),
    }


@router.post("/admin/proactive/scheduler/run-once")
async def admin_proactive_scheduler_run_once(
    limit: int = 20,
    bypass_quiet_hours: bool = False,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    result = await run_proactive_scheduler_once(
        batch_size=limit,
        bypass_quiet_hours=bypass_quiet_hours,
        planning_interval_seconds=settings.proactive_planning_interval_seconds,
        cleanup_app_notifications=cleanup_app_notifications_batch,
        notification_cleanup_batch_size=(
            settings.companion_world_notification_cleanup_batch_size
        ),
        # 手动 run-once 时强制跑一轮媒体回收（跳过小时节流），方便运营核对磁盘占用。
        reclaim_orphan_media=(
            functools.partial(reclaim_orphan_media_batch, force=True)
            if settings.has_central_role
            else None
        ),
    )
    dreaming = await asyncio.to_thread(run_daily_dreaming_scan, limit=limit)
    result["daily_dreaming"] = dreaming
    return {"status": "ok", "run": result}


@router.post("/admin/proactive/hot-topic/refresh-once")
async def admin_proactive_hot_topic_refresh_once(
    _: None = Depends(verify_admin_auth),
) -> dict:
    """手动触发一次全局热点池刷新（搜索→抽主题→入池）。受 settings 门控，幂等（当日已生成即 no_op）。"""
    result = await asyncio.to_thread(refresh_hot_topic_pool)
    return {"status": "ok", "run": result}


@router.post("/admin/accounts/{account_id}/hot-topic/select-once")
async def admin_proactive_hot_topic_select_once(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """对单账号跑一次热点选择（读全局池→LLM 打分→打散→top1），仅返回候选、不落库、不发送（调试用）。"""
    result = await asyncio.to_thread(select_hot_topic_candidate, account_id=account_id)
    return {"status": "ok", "run": result}
