"""Admin dreaming 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.products.zhaoxi.api.admin_dreaming.settings"。"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.config import settings
from app.routers.deps import verify_admin_auth
from app.db import get_account, get_dreaming_memory_item, get_dreaming_run, list_dreaming_memory_items, list_dreaming_runs, list_memory_events
from app.products.zhaoxi.application.memory.dreaming import rollback_memory_item, run_dreaming, summarize_dreaming_run_for_debug, summarize_memory_item_for_debug
from app.products.zhaoxi.jobs.dreaming.scheduler import get_dreaming_scheduler, run_dreaming_scheduler_once
from app.products.zhaoxi.application import build_companion_world_memory_sink, compact_companion_world_memory_batch
from datetime import date as date_cls
from typing import Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


@router.post("/admin/accounts/{account_id}/dreaming")
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
        memory_sink=build_companion_world_memory_sink(),
    )


@router.get("/admin/accounts/{account_id}/dreaming")
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


@router.get("/admin/dreaming/runs/{run_id}")
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


@router.get("/admin/dreaming/items")
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


@router.get("/admin/dreaming/items/{item_id}/events")
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


@router.post("/admin/dreaming/items/{item_id}/rollback")
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


@router.get("/admin/dreaming/scheduler")
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


@router.post("/admin/dreaming/scheduler/run-once")
async def admin_dreaming_scheduler_run_once(
    limit: int = 100,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    result = await run_dreaming_scheduler_once(
        batch_size=limit,
        memory_sink=build_companion_world_memory_sink(),
        memory_compactor=(
            compact_companion_world_memory_batch
            if settings.has_central_role
            else None
        ),
    )
    return {"status": "ok", "run": result}
