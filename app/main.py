import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import date as date_cls, datetime, timedelta, timezone

from app.time_utils import beijing_now, beijing_now_str, verify_host_timezone
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
    close_pg_pool,
    clear_session_messages,
    clear_all_messages_for_account,
    cancel_proactive_commitment,
    cleanup_app_notifications_batch,
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
    close_persistent_gateway_client,
    send_weixin_text,
    warmup_persistent_gateway_client,
)
from app.proactive.delivery.outbound import enqueue_onboarding_welcome
from app.dreaming_scheduler import (
    get_dreaming_scheduler,
    run_dreaming_scheduler_once,
    start_dreaming_scheduler,
    stop_dreaming_scheduler,
)
from app.platform import (
    build_companion_world_memory_sink,
    compact_companion_world_memory_batch,
)
from app.user_meta_scheduler import (
    start_user_meta_scheduler,
    stop_user_meta_scheduler,
)
from app.prompt_builder import PromptBuilder, extract_section
from app.proactive.orchestration.scheduler import (
    get_proactive_scheduler,
    run_proactive_scheduler_once,
    start_proactive_scheduler,
    stop_proactive_scheduler,
)
from app.proactive.recall.manual_companion import (
    clear_account_check_candidate_draft,
    generate_account_check_candidate_draft,
    promote_account_check_candidate_draft,
)
from app.proactive.recall.content_invitation import generate_content_invitation_candidate
from app.proactive.recall.topic_followup import generate_topic_followup_candidate
from app.proactive.delivery.account_check import (
    decide_account_check_action,
    execute_account_check_decision,
)
from app.proactive.store.candidates import (
    REACTIVATION_TYPES,
    get_reactivation_candidate_from_metadata,
)
from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
from app.proactive.orchestration.planning import plan_reactivation_candidate
from app.proactive.store.account_state import format_state_time
from app.proactive.preferences import (
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
from app.session_lifecycle import configure_memory_sink, run_daily_dreaming_scan
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
# 仅非生产环境可直接打开的调试 UI。prompt_debug 不在此列：它在生产经域名 /ops/ 访问，
# 已由 nginx basic auth + 页面内 admin Bearer token + 明文 allowlist 三层保护，且其底层
# /debug/* API 在生产本就可达，故放开该页 UI（其余调试页仍仅限非生产）。
_LOCAL_ONLY_DEBUG_UI_PATHS = {
    "/ui/onboarding_debug.html",
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


# 所有角色都需要：健康检查 + bridge（接收 OpenClaw/节点 turn）
from app.routers import health as _health_router, bridge as _bridge_router  # noqa: E402
app.include_router(_health_router.router)
app.include_router(_bridge_router.router)

# 中心/standalone 独有：静态前端、Web 注册、管理台、审核台等控制面路由
# 纯节点（AI4ALL_ROLE=node）不挂载，减少启动依赖与暴露面
if settings.has_central_role:
    app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
    app.mount("/ops", StaticFiles(directory="app/static", html=True), name="ops")
    from app.routers import web as _web_router  # noqa: E402
    app.include_router(_web_router.router)
    from app.routers import app_api as _app_api_router  # noqa: E402
    app.include_router(_app_api_router.router)
    from app.routers import companion_world as _companion_world_router  # noqa: E402
    app.include_router(_companion_world_router.router)
    _companion_world_router.install_exception_handlers(app)
    from app.routers import companion_world_mailbox as _companion_world_mailbox_router  # noqa: E402
    app.include_router(_companion_world_mailbox_router.router)
    from app.routers import companion_world_visits as _companion_world_visits_router  # noqa: E402
    app.include_router(_companion_world_visits_router.router)
    from app.routers import companion_world_human_chat as _companion_world_human_chat_router  # noqa: E402
    app.include_router(_companion_world_human_chat_router.router)
    from app.routers import app_notifications as _app_notifications_router  # noqa: E402
    app.include_router(_app_notifications_router.router)
    from app.routers import debug as _debug_router  # noqa: E402
    app.include_router(_debug_router.router)
    from app.routers import admin_moderation as _admin_moderation_router  # noqa: E402
    app.include_router(_admin_moderation_router.router)
    from app.routers import admin_accounts as _admin_accounts_router  # noqa: E402
    app.include_router(_admin_accounts_router.router)
    from app.routers import admin_proactive as _admin_proactive_router  # noqa: E402
    app.include_router(_admin_proactive_router.router)
    from app.routers import admin_dreaming as _admin_dreaming_router  # noqa: E402
    app.include_router(_admin_dreaming_router.router)
    from app.routers import admin_ops as _admin_ops_router  # noqa: E402
    app.include_router(_admin_ops_router.router)
    from app.routers import admin_llm as _admin_llm_router  # noqa: E402
    app.include_router(_admin_llm_router.router)
    from app.routers import admin_security as _admin_security_router  # noqa: E402
    app.include_router(_admin_security_router.router)
    from app.routers import admin_campaigns as _admin_campaigns_router  # noqa: E402
    app.include_router(_admin_campaigns_router.router)
    from app.routers import admin_companion_world as _admin_companion_world_router  # noqa: E402
    app.include_router(_admin_companion_world_router.router)

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








































def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()




















@app.on_event("startup")
def startup() -> None:
    # 主动消息分类 registry 一致性校验（enum/registry 对齐、source 唯一、豁免不变量）。
    # 放在最前：配置性错误应在启动即暴露，而非运行期静默错配配额/开关。
    from app.proactive.contract.categories import validate_category_registry

    validate_category_registry()
    if settings.has_central_role:
        # standalone / central：运行完整 DDL 迁移
        init_db()
    else:
        # 纯节点：仅验证 DB 连接可用，DDL 由中心执行，节点不重复跑迁移
        from app.db._core import connect
        with connect() as _conn:
            _conn.execute("SELECT 1")
    configure_error_log_alerting(settings)
    # 宿主机时区第二层防御：非 UTC+8 时报警但兼容继续（详见 time_utils.verify_host_timezone）。
    verify_host_timezone()


@app.on_event("startup")
async def capture_event_loop() -> None:
    global _background_loop
    _background_loop = asyncio.get_running_loop()
    set_background_loop(_background_loop)


@app.on_event("startup")
async def verify_tdai_multitenant() -> None:
    """多租户安全闸门：启用主动检索时确认网关处于强隔离模式。

    L1 检索的账号隔离完全依赖网关 multiTenant=true 的物理分库；网关若退回共享库模式，
    tdai_memory_search 会跨账号串号。探针 best-effort（网络错误不阻塞启动），确认共享库
    模式则强制关闭主动检索（recall/capture 不受影响）。
    """
    if not (getattr(settings, "tdai_enabled", False) and getattr(settings, "tdai_search_enabled", False)):
        return
    from app.tdai_client import mark_multitenant_unsafe, verify_multitenant

    ok = await asyncio.to_thread(verify_multitenant)
    if ok is False:
        logger.critical(
            "TDAI gateway 未处于 multiTenant 强隔离模式（缺 session_key 未返回 400），"
            "主动检索会跨账号串号，已强制关闭 tdai search 工具。"
        )
        mark_multitenant_unsafe()
    elif ok is None:
        logger.warning("TDAI multiTenant 探针未能确认（网络/超时），主动检索仍受显式开关+allowlist 双重 gating。")


@app.on_event("startup")
async def startup_persistent_gateway_client() -> None:
    if not getattr(settings, "openclaw_gateway_ws_warmup_on_startup", False):
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, warmup_persistent_gateway_client)
    except Exception as err:
        logger.warning("persistent OpenClaw Gateway warmup failed: %s", err)


@app.on_event("startup")
async def startup_proactive_scheduler() -> None:
    if not getattr(settings, "proactive_scheduler_enabled", False):
        return
    # 角色守卫(防御纵深):主动调度只在具备中心能力的机器跑。纯 node 上 dispatch 会把
    # 远程账号 enqueue 成 pending,而 reminders/commitments/reactivation 会把 pending
    # 当 failed/blocked 处理 → 误判。即便 node 误配 PROACTIVE_SCHEDULER_ENABLED=true 也不起。
    if not settings.has_central_role:
        logger.warning(
            "proactive scheduler skipped: AI4ALL_ROLE=%r 非 central/standalone,纯 node 不调度",
            settings.ai4all_role,
        )
        return
    scheduler = start_proactive_scheduler(
        interval_seconds=settings.proactive_scheduler_interval_seconds,
        batch_size=settings.proactive_scheduler_batch_size,
        bypass_quiet_hours=settings.proactive_scheduler_bypass_quiet_hours,
        planning_interval_seconds=settings.proactive_planning_interval_seconds,
        node_id=settings.node_id or None,
        cleanup_app_notifications=cleanup_app_notifications_batch,
        notification_cleanup_batch_size=(
            settings.companion_world_notification_cleanup_batch_size
        ),
    )
    logger.info("proactive scheduler started: %s", scheduler.status())


@app.on_event("startup")
async def startup_companion_world_memory_sink() -> None:
    """为 API 进程内的懒 session 轮转注入通用 typed memory sink。"""
    configure_memory_sink(build_companion_world_memory_sink())


@app.on_event("startup")
async def startup_dreaming_scheduler() -> None:
    if not getattr(settings, "dreaming_scheduler_enabled", False):
        return
    # 覆盖范围:dreaming 只压缩记忆、不发 openclaw → 节点无关,任何能连 PG 的机器都能
    # 为任意账号 dream。故具备中心能力的机器(standalone / central[,node])承担**全队列**
    # 每日扫描(node_id=None 扫全部账号),避免远程节点账号(如 aliyun2 归属的)漏扫;
    # 纯 node 才按 P4 分片只扫自身(assigned_node_id=本节点)。
    # ⚠️ 中心扫全量时各节点不要再单独开 dreaming 调度器,否则同账号会被两边重复扫描。
    daily_scan_node_id = None if settings.has_central_role else (settings.node_id or None)
    scheduler = start_dreaming_scheduler(
        batch_size=settings.dreaming_scheduler_batch_size,
        start_hour=settings.conversation_session_business_day_start_hour,
        node_id=daily_scan_node_id,
        memory_sink=build_companion_world_memory_sink(),
        memory_compactor=(
            compact_companion_world_memory_batch
            if settings.has_central_role
            else None
        ),
    )
    logger.info("dreaming scheduler started: %s", scheduler.status())


@app.on_event("startup")
async def startup_user_meta_scheduler() -> None:
    if not getattr(settings, "user_meta_scheduler_enabled", False):
        return
    scheduler = start_user_meta_scheduler(
        page_size=settings.user_meta_scheduler_page_size,
        inter_account_sleep=settings.user_meta_scheduler_inter_account_sleep,
        start_hour=settings.user_meta_scheduler_hour,
    )
    logger.info("user meta scheduler started: %s", scheduler.status())


@app.on_event("shutdown")
async def shutdown_proactive_scheduler() -> None:
    await stop_proactive_scheduler()


@app.on_event("shutdown")
async def shutdown_companion_world_memory_sink() -> None:
    configure_memory_sink(None)


@app.on_event("shutdown")
async def shutdown_dreaming_scheduler() -> None:
    await stop_dreaming_scheduler()


@app.on_event("shutdown")
async def shutdown_user_meta_scheduler() -> None:
    await stop_user_meta_scheduler()


@app.on_event("shutdown")
async def shutdown_persistent_gateway_client() -> None:
    try:
        close_persistent_gateway_client()
    except Exception as err:
        logger.warning("persistent OpenClaw Gateway close failed: %s", err)


@app.on_event("shutdown")
async def shutdown_db_pool() -> None:
    # 优雅关闭 PG 连接池（SQLite 部署为 no-op），避免重启时 PG 侧残留连接告警。
    try:
        close_pg_pool()
    except Exception as err:
        logger.warning("PG connection pool close failed: %s", err)






# ---------------------------------------------------------------------------
# Admin — moderation
# ---------------------------------------------------------------------------





































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

























































# ---------------------------------------------------------------------------
# Admin — proactive scheduler
# ---------------------------------------------------------------------------













# ---------------------------------------------------------------------------
# Admin — sessions
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Admin — messages
# ---------------------------------------------------------------------------





























# ---------------------------------------------------------------------------
# Bridge endpoint
# ---------------------------------------------------------------------------
