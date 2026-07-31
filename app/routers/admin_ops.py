"""Admin ops 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.routers.admin_ops.settings"。"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.config import settings
from app.routers.deps import require_admin_user, verify_admin_auth
from app.routers.serializers import _can_bypass_redaction_for_account, _debug_redaction_payload, _message_for_view, _profile_for_view, _redacted_flag_for_account, _session_for_view, _trace_for_view
from app.routers.health import _build_ready_status
from app.db import (
    clear_session_messages,
    get_debug_trace,
    get_inbound_message_rate,
    get_message_raw,
    get_ops_metrics,
    get_profile_for_session,
    get_recent_reply_latencies,
    get_session,
    get_today_inbound_message_rate,
    list_debug_traces,
    list_recent_message_raw,
    list_scheduler_heartbeats,
    list_session_messages,
    list_sessions,
)
from app.products.zhaoxi.jobs.user_meta.scheduler import get_user_meta_scheduler, run_user_meta_scheduler_once
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


@router.get("/admin/ops/status")
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
                # dreaming 是天级定点扫描，没有 interval 配置项；enabled 只代表 FastAPI
                # in-process 开关，线上实际由 proactive-scheduler 单例进程承担，故两个开关都要报。
                "dreaming": {
                    "enabled": settings.dreaming_scheduler_enabled,
                    "proactive_process_enabled": settings.proactive_dreaming_scheduler_enabled,
                    "batch_size": settings.dreaming_scheduler_batch_size,
                },
                "user_meta": {
                    "enabled": settings.user_meta_scheduler_enabled,
                    "hour": settings.user_meta_scheduler_hour,
                    "page_size": settings.user_meta_scheduler_page_size,
                    "inter_account_sleep": settings.user_meta_scheduler_inter_account_sleep,
                    "scheduler": (
                        get_user_meta_scheduler().status()
                        if get_user_meta_scheduler()
                        else None
                    ),
                },
                "world_lifecycle": {
                    "lifecycle_enabled": settings.companion_world_lifecycle_evaluation_enabled,
                    "mailbox_enabled": settings.companion_world_mailbox_enabled,
                    "visits_enabled": settings.companion_world_visits_enabled,
                    "wishes_enabled": settings.companion_world_mailbox_enabled,
                    "interval_seconds": settings.companion_world_lifecycle_scheduler_interval_seconds,
                    "batch_size": settings.companion_world_lifecycle_scheduler_batch_size,
                },
            },
            "heartbeats": list_scheduler_heartbeats(),
        },
        "metrics": get_ops_metrics(window_minutes=window_minutes),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/admin/ops/inbound-rate")
def admin_ops_inbound_rate(
    _: None = Depends(verify_admin_auth),
) -> dict:
    """实时入站监控：返回滚动窗口、今日窗口和最近回复延时，供后台页面轮询。"""
    return {
        "windows": get_inbound_message_rate(windows_minutes=(10, 60)),
        "today": get_today_inbound_message_rate(),
        "recent_reply_latencies": get_recent_reply_latencies(limit=10),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.post("/admin/ops/user-meta/run-once")
async def admin_user_meta_run_once(
    page_size: Optional[int] = None,
    inter_account_sleep: Optional[float] = None,
    _: dict = Depends(require_admin_user),
) -> dict:
    effective_page_size = page_size or settings.user_meta_scheduler_page_size
    if effective_page_size < 1 or effective_page_size > 1000:
        raise HTTPException(status_code=400, detail="page_size must be between 1 and 1000")
    effective_sleep = (
        settings.user_meta_scheduler_inter_account_sleep
        if inter_account_sleep is None
        else inter_account_sleep
    )
    if effective_sleep < 0 or effective_sleep > 60:
        raise HTTPException(status_code=400, detail="inter_account_sleep must be between 0 and 60")
    result = await run_user_meta_scheduler_once(
        page_size=effective_page_size,
        inter_account_sleep=effective_sleep,
    )
    return {"status": "ok", "run": result}


@router.get("/admin/sessions")
def admin_sessions(limit: int = 100, _: None = Depends(verify_admin_auth)) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@router.get("/admin/sessions/{session_id}")
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


@router.post("/admin/sessions/{session_id}/reset")
def admin_reset_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


@router.get("/admin/messages/raw")
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


@router.get("/admin/messages/{message_db_id}/raw")
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


@router.get("/admin/debug/traces")
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


@router.get("/admin/debug/traces/{trace_id}")
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
