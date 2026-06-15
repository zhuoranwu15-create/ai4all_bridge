import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import date as date_cls, datetime, timedelta, timezone

from app.time_utils import beijing_now, beijing_now_str
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.captcha import verify_captcha
from app.sms import generate_otp, send_otp
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    clear_session_messages,
    clear_all_messages_for_account,
    cancel_proactive_commitment,
    claim_content_moderation_task,
    count_verifications_last_hour,
    create_ai4all_account_for_user,
    create_admin_plaintext_grant,
    create_binding_intent,
    create_faq_message,
    create_search_provider_run,
    create_tool_invocation,
    get_or_create_personal_referral_code_for_user,
    create_phone_verification,
    connect as db_connect,
    find_active_admin_plaintext_grant,
    get_admin_plaintext_grant,
    get_debug_trace,
    get_dreaming_memory_item,
    get_dreaming_run,
    get_account,
    get_account_onboarding_state,
    get_admin_user as get_admin_user_record,
    get_binding_intent,
    get_daily_usage,
    get_content_moderation_export,
    get_content_moderation_stats,
    get_content_moderation_task,
    get_content_invitation,
    get_latest_active_verification,
    get_latest_subscription_for_user,
    get_message_raw,
    get_or_create_default_ai4all_account_for_user,
    get_platform_user,
    get_platform_user_by_phone,
    get_ops_metrics,
    get_profile_for_account,
    get_profile_for_session,
    get_proactive_commitment,
    get_session,
    get_or_create_session,
    get_usage_last_7_days,
    get_wallet_summary,
    increment_verify_attempts,
    init_db,
    invalidate_other_verifications_for_phone,
    invalidate_verification,
    insert_admin_access_event,
    insert_content_moderation_action,
    insert_debug_trace,
    list_accounts,
    list_admin_access_events,
    list_admin_plaintext_grants,
    list_admin_users,
    list_account_owner_bindings_for_account,
    list_binding_intents_for_account,
    list_content_invitations_for_account,
    list_content_moderation_actions,
    list_content_moderation_results,
    list_content_moderation_tasks,
    list_outbound_messages,
    list_reactivation_outbound_messages_admin,
    list_referral_relationships,
    list_published_faq_messages,
    list_proactive_commitments_for_account,
    list_debug_traces,
    list_dreaming_memory_items,
    list_dreaming_runs,
    list_memory_events,
    list_recent_message_raw,
    list_scheduler_heartbeats,
    list_session_messages,
    list_sessions,
    list_sessions_for_account,
    create_platform_user_session,
    get_platform_user_by_session_token,
    normalize_phone,
    resolve_account_id_for_inbound_channel_identity,
    set_account_onboarding_state,
    set_account_debug_flag,
    set_binding_intent_error,
    set_account_status,
    set_verification_verified,
    update_admin_plaintext_grant_status,
    update_binding_intent,
    update_account,
    update_content_moderation_task_review_status,
    update_moderation_account_risk_controls,
    update_profile_for_account,
    update_profile_for_session,
    get_proactive_account_state,
    list_proactive_message_setting_events,
    list_channel_bindings_for_account,
    upsert_proactive_account_state,
    upsert_channel_binding,
    upsert_admin_user,
    cancel_reminder,
    get_reminder,
    list_reminders_for_account,
    get_tool_invocation,
    like_faq_message,
    list_wallet_ledger,
    list_search_provider_runs,
    list_tool_invocations,
    update_reminder,
    update_tool_invocation,
    mark_referral_relationship_bound,
    preview_referral_code,
    register_platform_user_with_referral,
    release_due_referral_rewards,
    unbind_account_channel,
    validate_referral_code,
    wipe_account_data,
    unbind_and_wipe_account,
    reenable_proactive_after_rebind,
)
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.llm import generate_completion
from app.onboarding import ONBOARDING_STEP1_SENT, ONBOARDING_WELCOME_TEXT, is_onboarding_active
from app.openclaw_gateway import (
    send_weixin_text,
)
from app.proactive.messaging import enqueue_onboarding_welcome
from app.dreaming_scheduler import (
    get_dreaming_scheduler,
    run_dreaming_scheduler_once,
    start_dreaming_scheduler,
    stop_dreaming_scheduler,
)
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.scheduler import (
    get_proactive_scheduler,
    run_proactive_scheduler_once,
    start_proactive_scheduler,
    stop_proactive_scheduler,
)
from app.proactive.account_checks import (
    clear_account_check_candidate_draft,
    decide_account_check_action,
    execute_account_check_decision,
    generate_content_invitation_candidate,
    generate_account_check_candidate_draft,
    generate_topic_followup_candidate,
    promote_account_check_candidate_draft,
)
from app.proactive.reactivation import (
    REACTIVATION_TYPES,
    dispatch_reactivation_candidate,
    get_reactivation_candidate_from_metadata,
    plan_reactivation_candidate,
)
from app.proactive.state import format_state_time
from app.proactive.settings import (
    PROACTIVE_FREQUENCY_BUCKETS,
    apply_proactive_message_settings_patch,
    get_effective_proactive_message_settings,
    resolve_frequency_limits,
)
from app.schemas import (
    NodeHeartbeatRequest,
    NodeOutboundClaimRequest,
    NodeOutboundResultRequest,
    OpenClawDebugTraceRequest,
    OpenClawTurnRequest,
    OpenClawTurnResponse,
)
from app import node_gateway
from app.db import (
    claim_pending_outbound_by_node,
    insert_outbound_delivery_message,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
    resolve_node_for_account,
    should_inline_dispatch_for_account,
    upsert_access_node,
)
from app.dreaming import (
    rollback_memory_item,
    run_dreaming,
    summarize_dreaming_run_for_debug,
    summarize_memory_item_for_debug,
)
from app.session_lifecycle import run_daily_dreaming_scan
from app.turn_service import build_turn_llm_input, handle_openclaw_turn
from app.moderation import export as moderation_export
from app.tools import get_web_search_tools
from app.tools.web_search_handlers import handle_web_search, override_provider_order
from app.rate_limiter import RateLimiter
import shutil

import httpx

from app.user_profiles import (
    CONTEXT_FILE_ORDER,
    account_profile_dir,
    context_file_path,
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)
from app.alerting import configure_error_log_alerting
from app.app_runtime import set_background_loop, get_background_loop
from app.routers.deps import *  # noqa: F401,F403 鉴权依赖（搬出后回引，供留存 handler 用）
from app.routers.serializers import *  # noqa: F401,F403 序列化/脱敏 helper 回引
from app.routers.health import _build_ready_status
from app.routers.models import ProfileUpdateRequest  # admin/ops 状态接口复用就绪检查


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai4all")

_background_loop: Optional[asyncio.AbstractEventLoop] = None

app = FastAPI(title="AI4ALL Weixin Bot", version="0.1.0")

_LOCAL_DEBUG_UI_ENVS = {"local", "development", "test"}
_LOCAL_ONLY_DEBUG_UI_PATHS = {
    "/ui/onboarding_debug.html",
    "/ui/prompt_debug.html",
    "/ui/proactive_debug.html",
    "/ui/web_search_debug.html",
}


@app.middleware("http")
async def _gate_debug_ui(request: Request, call_next):
    if (
        request.url.path in _LOCAL_ONLY_DEBUG_UI_PATHS
        and str(settings.app_env or "").lower() not in _LOCAL_DEBUG_UI_ENVS
    ):
        return JSONResponse({"detail": "Not available"}, status_code=403)
    return await call_next(request)


class _ApiPrefixStripMiddleware:
    """Strip leading path prefixes added by nginx for routing.

    - ``/api/*`` → ``/*``: static frontend prefixes web API calls with ``/api``
      so nginx can route them to the backend; strip for local uvicorn access.
    - ``/ops/admin/*``, ``/ops/debug/*``, ``/ops/openclaw/*`` → ``/admin/*`` etc.:
      ops debug pages run JS that prefixes API calls with ``/ops`` (so nginx can
      route them); strip those prefixes for local uvicorn access too.
      Static file paths like ``/ops/proactive_debug.html`` are NOT matched and
      remain intact so the StaticFiles mount continues to serve them.
    """

    # API sub-paths that ops debug pages prefix with /ops
    _OPS_API_PREFIXES = ("/admin", "/debug", "/openclaw")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            new_path = None
            if path == "/api" or path.startswith("/api/"):
                new_path = path[4:] or "/"
            elif path.startswith("/ops/"):
                rest = path[4:]  # "/admin/..." etc.
                if any(rest == p or rest.startswith(p + "/") for p in self._OPS_API_PREFIXES):
                    new_path = rest
            if new_path is not None:
                scope = dict(scope)
                scope["path"] = new_path
                raw = scope.get("raw_path")
                if raw:
                    scope["raw_path"] = raw[len(path) - len(new_path):] or b"/"
        await self.app(scope, receive, send)


app.add_middleware(_ApiPrefixStripMiddleware)


app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
app.mount("/ops", StaticFiles(directory="app/static", html=True), name="ops")

from app.routers import health as _health_router, bridge as _bridge_router  # noqa: E402
app.include_router(_health_router.router)
app.include_router(_bridge_router.router)
from app.routers import web as _web_router  # noqa: E402
app.include_router(_web_router.router)
from app.routers import debug as _debug_router  # noqa: E402
app.include_router(_debug_router.router)

# Local-dev convenience: production nginx serves the static frontend at "/" and
# "/user/*" (the frontend hardcodes those absolute paths). Replicate that mapping
# only in local/dev envs so absolute links work when hitting uvicorn directly.
# Untouched in production, where nginx serves these paths and the backend never
# receives them.
if str(settings.app_env or "").lower() in _LOCAL_DEBUG_UI_ENVS:
    app.mount("/user", StaticFiles(directory="app/static", html=True), name="user_local")

    @app.get("/", include_in_schema=False)
    async def _local_root(request: Request) -> RedirectResponse:
        target = "/ui/home.html"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target)




class AccountUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None
    daily_limit: Optional[int] = None
    rpm_limit: Optional[int] = None


class ProactiveAccountStateUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    next_scan_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(default=None)


class PlaintextGrantRequest(BaseModel):
    reason: str
    account_scope: list[str] = Field(default_factory=list)
    resource_scope: list[str] = Field(default_factory=list)
    time_scope_start: Optional[str] = None
    time_scope_end: Optional[str] = None


class ModerationDecisionRequest(BaseModel):
    decision: str
    risk_categories: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    reason: Optional[str] = Field(default=None, max_length=1000)
    proactive_blocked_until: Optional[str] = None


class ModerationActionRequest(BaseModel):
    action: str
    reason: Optional[str] = Field(default=None, max_length=1000)
    proactive_blocked_until: Optional[str] = None


class ModerationExportRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ModerationPolicyUpdateRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)
























def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()




















@app.on_event("startup")
def startup() -> None:
    init_db()
    configure_error_log_alerting(settings)


@app.on_event("startup")
async def capture_event_loop() -> None:
    global _background_loop
    _background_loop = asyncio.get_running_loop()
    set_background_loop(_background_loop)


@app.on_event("startup")
async def startup_proactive_scheduler() -> None:
    if not getattr(settings, "proactive_scheduler_enabled", False):
        return
    scheduler = start_proactive_scheduler(
        interval_seconds=settings.proactive_scheduler_interval_seconds,
        batch_size=settings.proactive_scheduler_batch_size,
        bypass_quiet_hours=settings.proactive_scheduler_bypass_quiet_hours,
        planning_interval_seconds=settings.proactive_planning_interval_seconds,
    )
    logger.info("proactive scheduler started: %s", scheduler.status())


@app.on_event("startup")
async def startup_dreaming_scheduler() -> None:
    if not getattr(settings, "dreaming_scheduler_enabled", False):
        return
    scheduler = start_dreaming_scheduler(
        batch_size=settings.dreaming_scheduler_batch_size,
        start_hour=settings.conversation_session_business_day_start_hour,
    )
    logger.info("dreaming scheduler started: %s", scheduler.status())


@app.on_event("shutdown")
async def shutdown_proactive_scheduler() -> None:
    await stop_proactive_scheduler()


@app.on_event("shutdown")
async def shutdown_dreaming_scheduler() -> None:
    await stop_dreaming_scheduler()


@app.get("/admin/me")
def admin_me(admin_user: dict = Depends(get_admin_user)) -> dict:
    return {"admin_user": admin_user}


@app.get("/admin/ops/status")
def admin_ops_status(
    window_minutes: int = 60,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if window_minutes < 1 or window_minutes > 24 * 60:
        raise HTTPException(status_code=400, detail="window_minutes must be between 1 and 1440")
    ready_status_code, ready = _build_ready_status()
    return {
        "status": "ok",
        "env": settings.app_env,
        "ready_status_code": ready_status_code,
        "ready": ready,
        "schedulers": {
            "configured": {
                "proactive": {
                    "enabled": settings.proactive_scheduler_enabled,
                    "interval_seconds": settings.proactive_scheduler_interval_seconds,
                    "batch_size": settings.proactive_scheduler_batch_size,
                },
                "dreaming": {
                    "enabled": settings.dreaming_scheduler_enabled,
                    "interval_seconds": settings.dreaming_scheduler_interval_seconds,
                    "batch_size": settings.dreaming_scheduler_batch_size,
                },
            },
            "heartbeats": list_scheduler_heartbeats(),
        },
        "metrics": get_ops_metrics(window_minutes=window_minutes),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Admin — moderation
# ---------------------------------------------------------------------------

_MODERATION_REVIEWABLE_STATUSES = {"needs_review", "reviewing", "blocked", "escalated"}
_MODERATION_DECISION_STATUS = {
    "approved": "approved",
    "false_positive": "false_positive",
    "risk_confirmed": "risk_confirmed",
    "escalated": "escalated",
    "closed": "closed",
}
_MODERATION_DECISION_RISK_LEVEL = {
    "approved": "pass",
    "false_positive": "pass",
    "risk_confirmed": "block",
    "escalated": "escalate",
}
_MODERATION_ADMIN_ACTIONS = {"restrict_proactive", "disable_account", "block", "close"}


def _moderation_task_for_list(task: dict) -> dict:
    item = dict(task)
    snapshot = item.pop("snapshot_text", None)
    item["snapshot_text_chars"] = len(str(snapshot or ""))
    return _normalize_ts(item, "reviewed_at", "machine_claimed_at", "machine_completed_at")


def _moderation_task_detail_allowed(task: dict, admin_user: dict) -> bool:
    if admin_user.get("role") == "admin":
        return True
    assigned = task.get("assigned_admin_user_id")
    if assigned == admin_user.get("id"):
        return True
    return assigned is None and task.get("status") in _MODERATION_REVIEWABLE_STATUSES


def _require_moderation_task_access(task: Optional[dict], admin_user: dict) -> dict:
    if task is None:
        raise HTTPException(status_code=404, detail="moderation task not found")
    if not _moderation_task_detail_allowed(task, admin_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="moderation task is assigned to another reviewer")
    return task


def _audit_moderation_event(
    *,
    admin_user: dict,
    task: dict,
    action: str,
    request_path: str,
    plaintext: bool = False,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action=f"moderation.{action}",
        resource_type="moderation_task",
        resource_id=str(task["id"]),
        account_id=str(task["account_id"]),
        plaintext=plaintext,
        reason=reason,
        request_path=request_path,
        metadata=metadata,
    )


def _clean_moderation_categories(categories: list[str]) -> list[str]:
    cleaned = []
    for category in categories or []:
        text = str(category or "").strip()
        if text:
            cleaned.append(text)
    return list(dict.fromkeys(cleaned))


def _default_proactive_blocked_until() -> str:
    return (beijing_now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def _apply_moderation_admin_action(
    *,
    task: dict,
    action: str,
    admin_user: dict,
    reason: Optional[str],
    proactive_blocked_until: Optional[str] = None,
    request_path: str,
) -> dict:
    if admin_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required for moderation action")
    clean_action = str(action or "").strip()
    if clean_action not in _MODERATION_ADMIN_ACTIONS:
        raise HTTPException(status_code=400, detail="unsupported moderation action")

    previous_status = str(task.get("status") or "")
    next_status = previous_status
    metadata: dict[str, Any] = {}
    updated_task = task

    if clean_action == "restrict_proactive":
        until = _normalize_optional_state_datetime(
            proactive_blocked_until or _default_proactive_blocked_until(),
            field_name="proactive_blocked_until",
        )
        update_moderation_account_risk_controls(
            account_id=str(task["account_id"]),
            risk_level="restricted",
            proactive_blocked_until=until,
            metadata_patch={
                "last_admin_action": clean_action,
                "last_admin_task_id": task["id"],
            },
        )
        settings_result = apply_proactive_message_settings_patch(
            account_id=str(task["account_id"]),
            patch={"muted_until": until},
            source="admin",
            reason=reason or f"moderation task {task['id']}",
        )
        metadata = {
            "proactive_blocked_until": until,
            "changed_fields": settings_result.get("changed_fields") or [],
        }
    elif clean_action == "disable_account":
        account = set_account_status(account_id=str(task["account_id"]), status="disabled")
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        update_moderation_account_risk_controls(
            account_id=str(task["account_id"]),
            risk_level="disabled",
            conversation_blocked_until=None,
            metadata_patch={
                "last_admin_action": clean_action,
                "last_admin_task_id": task["id"],
            },
        )
        metadata = {"account_status": account.get("status")}
    elif clean_action == "block":
        next_status = "blocked"
        updated_task = update_content_moderation_task_review_status(
            task_id=str(task["id"]),
            status=next_status,
            reviewed_by_admin_user_id=str(admin_user.get("id") or ""),
            reviewed_at=_now_db_time(),
        ) or task
    elif clean_action == "close":
        next_status = "closed"
        updated_task = update_content_moderation_task_review_status(
            task_id=str(task["id"]),
            status=next_status,
            reviewed_by_admin_user_id=str(admin_user.get("id") or ""),
            reviewed_at=_now_db_time(),
        ) or task

    moderation_action = insert_content_moderation_action(
        task_id=str(task["id"]),
        account_id=str(task["account_id"]),
        admin_user_id=str(admin_user.get("id") or ""),
        action=clean_action,
        previous_status=previous_status,
        next_status=next_status,
        reason=reason,
        metadata=metadata,
    )
    _audit_moderation_event(
        admin_user=admin_user,
        task=task,
        action=clean_action,
        request_path=request_path,
        plaintext=False,
        reason=reason,
        metadata=metadata,
    )
    return {"task": updated_task, "action": moderation_action}


@app.get("/admin/moderation/tasks")
def admin_moderation_tasks(
    account_id: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    risk_level: Optional[str] = None,
    direction: Optional[str] = None,
    content_kind: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 100,
    admin_user: dict = Depends(require_reviewer_or_admin),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    reviewer_scope = None if admin_user.get("role") == "admin" else str(admin_user.get("id"))
    tasks = list_content_moderation_tasks(
        account_id=account_id,
        status=status_filter,
        risk_level=risk_level,
        direction=direction,
        content_kind=content_kind,
        source_type=source_type,
        review_queue_or_assigned_admin_user_id=reviewer_scope,
        limit=limit,
    )
    return {"tasks": [_moderation_task_for_list(task) for task in tasks], "redacted": True}


@app.get("/admin/moderation/tasks/{task_id}")
def admin_moderation_task_detail(
    task_id: str,
    request: Request,
    reason: Optional[str] = None,
    admin_user: dict = Depends(require_reviewer_or_admin),
) -> dict:
    task = _require_moderation_task_access(
        get_content_moderation_task(task_id=task_id),
        admin_user,
    )
    _audit_moderation_event(
        admin_user=admin_user,
        task=task,
        action="view_task",
        request_path=request.url.path,
        plaintext=True,
        reason=reason or "moderation_task_detail",
    )
    return {
        "task": _normalize_ts(task, "reviewed_at", "machine_claimed_at", "machine_completed_at"),
        "results": list_content_moderation_results(task_id=task_id),
        "actions": list_content_moderation_actions(task_id=task_id),
        "plaintext": True,
    }


@app.post("/admin/moderation/tasks/{task_id}/claim")
def admin_moderation_claim_task(
    task_id: str,
    request: Request,
    admin_user: dict = Depends(require_reviewer_or_admin),
) -> dict:
    task = _require_moderation_task_access(
        get_content_moderation_task(task_id=task_id),
        admin_user,
    )
    if task.get("status") == "reviewing" and task.get("assigned_admin_user_id") != admin_user.get("id"):
        raise HTTPException(status_code=409, detail="moderation task is already assigned")
    if task.get("status") not in _MODERATION_REVIEWABLE_STATUSES:
        raise HTTPException(status_code=400, detail="moderation task is not reviewable")
    updated = claim_content_moderation_task(task_id=task_id, admin_user_id=str(admin_user.get("id") or ""))
    if updated is None:
        raise HTTPException(status_code=404, detail="moderation task not found")
    if updated.get("assigned_admin_user_id") != admin_user.get("id"):
        raise HTTPException(status_code=409, detail="moderation task is already assigned")
    if updated.get("status") != "reviewing":
        raise HTTPException(status_code=400, detail="moderation task is not reviewable")
    action = insert_content_moderation_action(
        task_id=task_id,
        account_id=str(task["account_id"]),
        admin_user_id=str(admin_user.get("id") or ""),
        action="claim",
        previous_status=str(task.get("status") or ""),
        next_status="reviewing",
        metadata={"assigned_admin_user_id": str(admin_user.get("id") or "")},
    )
    _audit_moderation_event(
        admin_user=admin_user,
        task=task,
        action="claim",
        request_path=request.url.path,
        metadata={"action_id": action["id"]},
    )
    return {"status": "ok", "task": updated, "action": action}


@app.post("/admin/moderation/tasks/{task_id}/decision")
def admin_moderation_decision(
    task_id: str,
    payload: ModerationDecisionRequest,
    request: Request,
    admin_user: dict = Depends(require_reviewer_or_admin),
) -> dict:
    task = _require_moderation_task_access(
        get_content_moderation_task(task_id=task_id),
        admin_user,
    )
    if task.get("status") != "reviewing":
        raise HTTPException(status_code=400, detail="moderation task must be claimed before decision")
    if task.get("assigned_admin_user_id") not in (None, admin_user.get("id")) and admin_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="moderation task is assigned to another reviewer")

    decision = str(payload.decision or "").strip()
    if decision not in _MODERATION_DECISION_STATUS:
        raise HTTPException(status_code=400, detail="invalid moderation decision")
    categories = _clean_moderation_categories(payload.risk_categories)
    if decision == "risk_confirmed" and not categories:
        raise HTTPException(status_code=400, detail="risk_confirmed requires risk_categories")
    admin_actions = [str(action or "").strip() for action in payload.actions or [] if str(action or "").strip()]
    if admin_actions and admin_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required for moderation actions")

    next_status = _MODERATION_DECISION_STATUS[decision]
    next_risk_level = _MODERATION_DECISION_RISK_LEVEL.get(decision, str(task.get("risk_level") or "unknown"))
    updated = update_content_moderation_task_review_status(
        task_id=task_id,
        status=next_status,
        risk_level=next_risk_level,
        risk_categories=categories if categories else task.get("risk_categories") or [],
        reviewed_by_admin_user_id=str(admin_user.get("id") or ""),
        reviewed_at=_now_db_time(),
    )
    action = insert_content_moderation_action(
        task_id=task_id,
        account_id=str(task["account_id"]),
        admin_user_id=str(admin_user.get("id") or ""),
        action=decision,
        previous_status=str(task.get("status") or ""),
        next_status=next_status,
        reason=payload.reason,
        metadata={"risk_categories": categories},
    )
    _audit_moderation_event(
        admin_user=admin_user,
        task=task,
        action=decision,
        request_path=request.url.path,
        plaintext=True,
        reason=payload.reason,
        metadata={"action_id": action["id"], "risk_categories": categories},
    )

    applied_actions = []
    latest_task = updated or task
    for admin_action in admin_actions:
        result = _apply_moderation_admin_action(
            task=latest_task,
            action=admin_action,
            admin_user=admin_user,
            reason=payload.reason,
            proactive_blocked_until=payload.proactive_blocked_until,
            request_path=request.url.path,
        )
        applied_actions.append(result["action"])
        latest_task = result["task"]
    if decision == "risk_confirmed":
        update_moderation_account_risk_controls(
            account_id=str(task["account_id"]),
            risk_level="elevated",
            metadata_patch={
                "last_human_task_id": task_id,
                "last_human_categories": categories,
            },
        )
    return {
        "status": "ok",
        "task": latest_task,
        "decision_action": action,
        "applied_actions": applied_actions,
    }


@app.post("/admin/moderation/tasks/{task_id}/actions")
def admin_moderation_task_action(
    task_id: str,
    payload: ModerationActionRequest,
    request: Request,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    task = get_content_moderation_task(task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="moderation task not found")
    result = _apply_moderation_admin_action(
        task=task,
        action=payload.action,
        admin_user=admin_user,
        reason=payload.reason,
        proactive_blocked_until=payload.proactive_blocked_until,
        request_path=request.url.path,
    )
    return {"status": "ok", **result}


@app.post("/admin/moderation/tasks/{task_id}/export")
def admin_moderation_export_task(
    task_id: str,
    payload: ModerationExportRequest,
    request: Request,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    try:
        result = moderation_export.create_export(
            task_id=task_id,
            admin_user=admin_user,
            reason=payload.reason,
            request_path=request.url.path,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="moderation task not found")
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"status": "ok", "export": result["export"]}


@app.get("/admin/moderation/exports/{export_id}")
def admin_moderation_get_export(
    export_id: str,
    request: Request,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    export = get_content_moderation_export(export_id=export_id)
    if export is None:
        raise HTTPException(status_code=404, detail="moderation export not found")
    try:
        artifact = moderation_export.load_export_artifact(export_id=export_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="moderation export artifact not found")
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action="moderation.view_export",
        resource_type="moderation_export",
        resource_id=export_id,
        account_id=str(export["account_id"]),
        plaintext=True,
        request_path=request.url.path,
        metadata={"task_id": export.get("task_id")},
    )
    return {"export": export, "artifact": artifact, "plaintext": True}


@app.get("/admin/moderation/stats")
def admin_moderation_stats(
    account_id: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if admin_user.get("role") not in {"admin", "reviewer", "staff"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin, reviewer or staff role required")
    return {"stats": get_content_moderation_stats(account_id=account_id)}


@app.get("/admin/moderation/policy")
def admin_moderation_policy(admin_user: dict = Depends(require_reviewer_or_admin)) -> dict:
    return {
        "moderation_enabled": bool(getattr(settings, "moderation_enabled", True)),
        "sync_guard_enabled": bool(getattr(settings, "moderation_sync_guard_enabled", True)),
        "worker_enabled": bool(getattr(settings, "moderation_worker_enabled", False)),
        "sample_percent": {
            "inbound": int(getattr(settings, "moderation_inbound_sample_percent", 15) or 15),
            "outbound": int(getattr(settings, "moderation_outbound_sample_percent", 30) or 30),
            "proactive": int(getattr(settings, "moderation_proactive_sample_percent", 100) or 100),
        },
        "short_text_skip_chars": int(getattr(settings, "moderation_short_text_skip_chars", 8) or 8),
        "llm_enabled": bool(getattr(settings, "moderation_llm_enabled", False)),
        "image_safety_enabled": bool(getattr(settings, "moderation_image_safety_enabled", False)),
    }


@app.put("/admin/moderation/policy")
def admin_moderation_update_policy(
    payload: ModerationPolicyUpdateRequest,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action="moderation.policy_update_requested",
        resource_type="moderation_policy",
        resource_id="runtime_settings",
        plaintext=False,
        reason=payload.reason,
        request_path="/admin/moderation/policy",
        metadata={"implemented": False},
    )
    raise HTTPException(status_code=501, detail="runtime moderation policy update is not implemented")


# ---------------------------------------------------------------------------
# Debug (admin auth required)
# ---------------------------------------------------------------------------















































# ---------------------------------------------------------------------------
# Reminder debug routes
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Web Search debug routes
# ---------------------------------------------------------------------------

























# ---------------------------------------------------------------------------
# Web onboarding (MVP)
# ---------------------------------------------------------------------------





































































# ---------------------------------------------------------------------------
# Admin — accounts
# ---------------------------------------------------------------------------

@app.get("/admin/accounts")
def admin_accounts(_: None = Depends(verify_admin_auth)) -> dict:
    return {"accounts": [_normalize_ts(a) for a in list_accounts()]}


@app.get("/admin/accounts/{account_id}")
def admin_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    owner_bindings = list_account_owner_bindings_for_account(account_id=account_id)
    active_owner = next((binding for binding in owner_bindings if binding.get("status") == "active"), None)
    platform_user = (
        get_platform_user(platform_user_id=str(active_owner["platform_user_id"]))
        if active_owner else None
    )
    binding_intents = [
        _binding_intent_for_view(intent, account_id=account_id)
        for intent in list_binding_intents_for_account(account_id=account_id)
    ]
    recent_traces = list_debug_traces(account_id=account_id, limit=10)
    return {
        "account": _normalize_ts(account),
        "platform_user": _platform_user_for_view(platform_user, account_id=account_id),
        "owner_bindings": owner_bindings,
        "binding_intents": binding_intents,
        "channel_bindings": [_normalize_ts(b) for b in list_channel_bindings_for_account(account_id=account_id)],
        "profile": _profile_for_view(
            get_profile_for_account(account_id=account_id) or {},
            account_id=account_id,
        ),
        "sessions": [_normalize_ts(s) for s in list_sessions_for_account(account_id=account_id)],
        "recent_traces": [_trace_for_view(trace) for trace in recent_traces],
        **(
            _debug_redaction_payload(account_id=account_id)
            if _can_bypass_redaction_for_account(account_id)
            else {"redacted": True}
        ),
    }


@app.patch("/admin/accounts/{account_id}")
def admin_update_account(
    account_id: str,
    payload: AccountUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    updates = payload.model_dump(exclude_unset=True)
    account = update_account(account_id=account_id, **updates)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.post("/admin/accounts/{account_id}/disable")
def admin_disable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="disabled")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.post("/admin/accounts/{account_id}/enable")
def admin_enable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="active")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@app.patch("/admin/accounts/{account_id}/profile")
def admin_update_account_profile(
    account_id: str,
    payload: ProfileUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    profile = update_profile_for_account(
        account_id=account_id,
        display_name=payload.display_name,
        style=payload.style,
        system_prompt=payload.system_prompt,
        preferences=payload.preferences,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="account/profile not found")
    return {"status": "ok", "profile": profile}


@app.get("/admin/accounts/{account_id}/sessions")
def admin_account_sessions(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"sessions": list_sessions_for_account(account_id=account_id, limit=limit)}


@app.get("/admin/accounts/{account_id}/user-profile")
def admin_get_user_profile(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "path": str(path),
            "content": content,
            "agent_context": context.metadata(),
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "path": str(path),
        "content_redacted": True,
        "content_chars": len(content),
        "agent_context": context.metadata(),
        "redacted": True,
    }


@app.get("/admin/accounts/{account_id}/context-files")
def admin_get_context_files(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """返回账号的 SOUL/IDENTITY/USER/MEMORY 上下文文件正文，供运维调试人设与记忆。

    这些是账号级 AI 上下文配置文件，对登录 admin 直接以明文返回（与原 AI 配置卡片
    展示 system_prompt 的口径一致）。
    """
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    context = read_agent_context(account_id)
    targets = ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")
    files = []
    for fname in targets:
        key = fname[:-3]
        meta = context.files.get(fname, {})
        files.append(
            {
                "file": fname,
                "key": key,
                "path": meta.get("path"),
                "exists": bool(meta.get("exists")),
                "chars": int(meta.get("chars") or 0),
                "content": context.blocks.get(key, ""),
            }
        )
    return {"account_id": account_id, "files": files}


@app.get("/admin/accounts/{account_id}/usage")
def admin_account_usage(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    today = date_cls.today().isoformat()
    return {
        "account_id": account_id,
        "today": {
            "date": today,
            "message_count": get_daily_usage(account_id=account_id, date=today),
        },
        "last_7_days": get_usage_last_7_days(account_id=account_id),
    }


@app.get("/admin/accounts/{account_id}/wallet")
def admin_account_wallet(
    account_id: str,
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    wallet = get_wallet_summary(
        account_id=account_id,
        ensure_grant=False,
        create_if_missing=False,
    )
    return {
        "account_id": account_id,
        "wallet": wallet,
        "ledger": list_wallet_ledger(account_id=account_id, limit=limit) if wallet else [],
        "redacted": True,
    }


@app.get("/admin/referrals")
def admin_referrals(
    limit: int = 50,
    inviter_platform_user_id: Optional[str] = None,
    invitee_platform_user_id: Optional[str] = None,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    return {
        "status": "ok",
        "referrals": list_referral_relationships(
            limit=limit,
            inviter_platform_user_id=inviter_platform_user_id,
            invitee_platform_user_id=invitee_platform_user_id,
        ),
        "redacted": True,
    }


@app.post("/admin/referrals/release-due-rewards")
def admin_release_due_referral_rewards(
    limit: int = 200,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    ledgers = release_due_referral_rewards(limit=limit)
    return {
        "status": "ok",
        "released_count": len(ledgers),
        "ledger": ledgers,
        "redacted": True,
    }


@app.get("/admin/accounts/{account_id}/proactive-overview")
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


@app.get("/admin/accounts/{account_id}/proactive-state")
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


@app.patch("/admin/accounts/{account_id}/proactive-state")
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


@app.post("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_generate_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = generate_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.post("/admin/accounts/{account_id}/proactive-check-candidate-draft/promote")
def admin_promote_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = promote_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.delete("/admin/accounts/{account_id}/proactive-check-candidate-draft")
def admin_clear_account_check_candidate_draft(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    result = clear_account_check_candidate_draft(account_id=account_id)
    return {"status": "ok", "result": result}


@app.post("/admin/accounts/{account_id}/proactive-check/run-once")
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


@app.get("/admin/accounts/{account_id}/commitments")
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


@app.post("/admin/commitments/{commitment_id}/cancel")
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


@app.post("/admin/accounts/{account_id}/dreaming")
def admin_run_account_dreaming(
    account_id: str,
    days: int = 7,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return run_dreaming(
        account_id=account_id,
        today=date_cls.today().isoformat(),
        days=days,
        source_type="manual_admin",
        actor_type="admin",
        actor_id="admin_api",
    )


@app.get("/admin/accounts/{account_id}/dreaming")
def admin_list_account_dreaming(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    runs = list_dreaming_runs(account_id=account_id, limit=limit)
    items = list_dreaming_memory_items(account_id=account_id, limit=limit)
    return {
        "account_id": account_id,
        "runs": [summarize_dreaming_run_for_debug(run) for run in runs],
        "items": [summarize_memory_item_for_debug(item) for item in items],
        "redacted": True,
    }


@app.get("/admin/dreaming/runs/{run_id}")
def admin_get_dreaming_run(
    run_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    run = get_dreaming_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="dreaming run not found")
    items = list_dreaming_memory_items(dreaming_run_id=run_id, limit=200)
    return {
        "run": summarize_dreaming_run_for_debug(run),
        "items": [summarize_memory_item_for_debug(item) for item in items],
        "redacted": True,
    }


@app.get("/admin/dreaming/items")
def admin_list_dreaming_items(
    account_id: Optional[str] = None,
    apply_status: Optional[str] = None,
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "items": [
            summarize_memory_item_for_debug(item)
            for item in list_dreaming_memory_items(
                account_id=account_id,
                apply_status=apply_status,
                limit=limit,
            )
        ],
        "redacted": True,
    }


@app.get("/admin/dreaming/items/{item_id}/events")
def admin_list_memory_item_events(
    item_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_dreaming_memory_item(item_id=item_id) is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    events = list_memory_events(memory_item_id=item_id, limit=50)
    return {
        "item_id": item_id,
        "events": [
            {
                "id": event["id"],
                "account_id": event["account_id"],
                "memory_item_id": event.get("memory_item_id"),
                "event_type": event["event_type"],
                "actor_type": event["actor_type"],
                "actor_id": event.get("actor_id"),
                "diff_chars": len(event.get("diff_text") or ""),
                "created_at": event.get("created_at"),
            }
            for event in events
        ],
        "redacted": True,
    }


@app.post("/admin/dreaming/items/{item_id}/rollback")
def admin_rollback_memory_item(
    item_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    result = rollback_memory_item(
        item_id=item_id,
        actor_type="admin",
        actor_id="admin_api",
    )
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="memory item not found")
    if result.get("item"):
        result["item"] = summarize_memory_item_for_debug(result["item"])
        result["redacted"] = True
    return result


# ---------------------------------------------------------------------------
# Admin — proactive scheduler
# ---------------------------------------------------------------------------

@app.get("/admin/proactive/scheduler")
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


@app.get("/admin/proactive/reactivation-candidates")
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


@app.get("/admin/proactive/reactivation-history")
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


@app.post("/admin/proactive/scheduler/run-once")
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
    )
    dreaming = await asyncio.to_thread(run_daily_dreaming_scan, limit=limit)
    result["daily_dreaming"] = dreaming
    return {"status": "ok", "run": result}


@app.get("/admin/dreaming/scheduler")
def admin_dreaming_scheduler_status(_: None = Depends(verify_admin_auth)) -> dict:
    scheduler = get_dreaming_scheduler()
    return {
        "enabled": bool(getattr(settings, "dreaming_scheduler_enabled", False)),
        "configured": {
            "batch_size": settings.dreaming_scheduler_batch_size,
            "business_day_start_hour": settings.conversation_session_business_day_start_hour,
        },
        "scheduler": scheduler.status() if scheduler else None,
    }


@app.post("/admin/dreaming/scheduler/run-once")
async def admin_dreaming_scheduler_run_once(
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    result = await run_dreaming_scheduler_once(batch_size=limit)
    return {"status": "ok", "run": result}


# ---------------------------------------------------------------------------
# Admin — sessions
# ---------------------------------------------------------------------------

@app.get("/admin/sessions")
def admin_sessions(limit: int = 100, _: None = Depends(verify_admin_auth)) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@app.get("/admin/sessions/{session_id}")
def admin_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    session = get_session(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    account_id = session.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "session": _session_for_view(session),
        "profile": _profile_for_view(
            get_profile_for_session(session_id=session_id) or {},
            account_id=account_id,
        ),
        "messages": [
            _message_for_view(message, account_id=account_id)
            for message in list_session_messages(session_id=session_id, limit=100)
        ],
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.post("/admin/sessions/{session_id}/reset")
def admin_reset_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


# ---------------------------------------------------------------------------
# Admin — messages
# ---------------------------------------------------------------------------

@app.get("/admin/messages/raw")
def admin_recent_message_raw(
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    messages = list_recent_message_raw(limit=limit)
    return {
        "messages": [
            _message_for_view(message)
            for message in messages
        ],
        "redacted": not any(_can_bypass_redaction_for_account(message.get("account_id")) for message in messages),
    }


@app.get("/admin/messages/{message_db_id}/raw")
def admin_message_raw(
    message_db_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    account_id = message.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "message": _message_for_view(message),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/admin/debug/traces")
def admin_debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": [
            _trace_for_view(t) for t in list_debug_traces(
                account_id=account_id,
                session_id=session_id,
                limit=limit,
            )
        ]
    }


@app.get("/admin/debug/traces/{trace_id}")
def admin_debug_trace(
    trace_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    account_id = trace.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "trace": _trace_for_view(trace),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@app.get("/admin/plaintext/messages/{message_db_id}/raw")
def admin_plaintext_message_raw(
    message_db_id: int,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=str(message.get("account_id")),
        resource_type="message",
    )
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_message_raw",
        resource_type="message",
        resource_id=str(message_db_id),
        account_id=message.get("account_id"),
        request_path=f"/admin/plaintext/messages/{message_db_id}/raw",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {"message": message, "plaintext": True}


@app.get("/admin/plaintext/debug-traces/{trace_id}")
def admin_plaintext_debug_trace(
    trace_id: str,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=str(trace.get("account_id")),
        resource_type="debug_trace",
    )
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_debug_trace",
        resource_type="debug_trace",
        resource_id=trace_id,
        account_id=trace.get("account_id"),
        request_path=f"/admin/plaintext/debug-traces/{trace_id}",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {"trace": trace, "plaintext": True}


@app.get("/admin/plaintext/accounts/{account_id}/user-profile")
def admin_plaintext_user_profile(
    account_id: str,
    reason: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    grant = _require_plaintext_access(
        admin_user=admin_user,
        account_id=account_id,
        resource_type="user_profile",
    )
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    _audit_plaintext_access(
        admin_user=admin_user,
        action="view_user_profile",
        resource_type="user_profile",
        resource_id=account_id,
        account_id=account_id,
        request_path=f"/admin/plaintext/accounts/{account_id}/user-profile",
        reason=reason,
        grant_id=int(grant["id"]) if grant else None,
    )
    return {
        "account_id": account_id,
        "path": str(path),
        "content": path.read_text(encoding="utf-8") if path.exists() else "",
        "agent_context": context.metadata(),
        "plaintext": True,
    }


@app.get("/admin/access-events")
def admin_access_events(
    account_id: Optional[str] = None,
    plaintext: Optional[bool] = None,
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {
        "events": list_admin_access_events(
            account_id=account_id,
            plaintext=plaintext,
            limit=limit,
        )
    }


@app.get("/admin/users")
def admin_users(
    limit: int = 100,
    _: dict = Depends(require_admin_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {"admin_users": list_admin_users(limit=limit)}


@app.post("/admin/plaintext-grants")
def admin_create_plaintext_grant(
    payload: PlaintextGrantRequest,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    reason = str(payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    account_scope = _clean_scope_list(payload.account_scope, field_name="account_scope")
    resource_scope = _clean_scope_list(payload.resource_scope, field_name="resource_scope")
    grant = create_admin_plaintext_grant(
        requester_admin_user_id=str(admin_user["id"]),
        reason=reason,
        account_scope=account_scope,
        resource_scope=resource_scope,
        time_scope_start=_normalize_optional_state_datetime(
            payload.time_scope_start,
            field_name="time_scope_start",
        ),
        time_scope_end=_normalize_optional_state_datetime(
            payload.time_scope_end,
            field_name="time_scope_end",
        ),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="request_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant["id"]),
        account_id=account_scope[0] if len(account_scope) == 1 else None,
        plaintext=False,
        reason=reason,
        request_path="/admin/plaintext-grants",
        metadata={"account_scope": account_scope, "resource_scope": resource_scope},
    )
    return {"status": "ok", "grant": grant}


@app.get("/admin/plaintext-grants")
def admin_list_plaintext_grants(
    requester_admin_user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    requester = requester_admin_user_id
    if admin_user.get("role") != "admin":
        requester = str(admin_user["id"])
    return {
        "grants": list_admin_plaintext_grants(
            requester_admin_user_id=requester,
            status=status,
            limit=limit,
        )
    }


@app.post("/admin/plaintext-grants/{grant_id}/approve")
def admin_approve_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending grants can be approved")
    now = beijing_now()
    approved_at = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="approved",
        approver_admin_user_id=str(admin_user["id"]),
        approved_at=approved_at,
        expires_at=expires_at,
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="approve_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        account_id=updated["account_scope"][0] if updated and len(updated.get("account_scope") or []) == 1 else None,
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/approve",
    )
    return {"status": "ok", "grant": updated}


@app.post("/admin/plaintext-grants/{grant_id}/reject")
def admin_reject_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending grants can be rejected")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="rejected",
        approver_admin_user_id=str(admin_user["id"]),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="reject_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/reject",
    )
    return {"status": "ok", "grant": updated}


@app.post("/admin/plaintext-grants/{grant_id}/revoke")
def admin_revoke_plaintext_grant(
    grant_id: int,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    grant = get_admin_plaintext_grant(grant_id=grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="plaintext grant not found")
    if grant["status"] not in {"pending", "approved"}:
        raise HTTPException(status_code=400, detail="only pending or approved grants can be revoked")
    updated = update_admin_plaintext_grant_status(
        grant_id=grant_id,
        status="revoked",
        approver_admin_user_id=str(admin_user["id"]),
        revoked_at=_now_db_time(),
    )
    insert_admin_access_event(
        admin_user_id=admin_user["id"],
        action="revoke_plaintext_grant",
        resource_type="plaintext_grant",
        resource_id=str(grant_id),
        plaintext=False,
        request_path=f"/admin/plaintext-grants/{grant_id}/revoke",
    )
    return {"status": "ok", "grant": updated}


# ---------------------------------------------------------------------------
# Bridge endpoint
# ---------------------------------------------------------------------------

