"""Admin moderation 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.routers.admin_moderation.settings"。"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from app.config import settings
from app.routers.deps import get_admin_user, require_admin_user, require_reviewer_or_admin
from app.routers.serializers import _normalize_optional_state_datetime, _normalize_ts, _now_db_time
from app.db import claim_content_moderation_task, get_content_moderation_export, get_content_moderation_stats, get_content_moderation_task, insert_admin_access_event, insert_content_moderation_action, list_content_moderation_actions, list_content_moderation_results, list_content_moderation_tasks, set_account_status, update_content_moderation_task_review_status, update_moderation_account_risk_controls
from app.moderation import export as moderation_export
from app.proactive.preferences import apply_proactive_message_settings_patch
from app.time_utils import beijing_now
from datetime import timedelta
from typing import Any, Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


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


@router.get("/admin/moderation/tasks")
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


@router.get("/admin/moderation/tasks/{task_id}")
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


@router.post("/admin/moderation/tasks/{task_id}/claim")
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


@router.post("/admin/moderation/tasks/{task_id}/decision")
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


@router.post("/admin/moderation/tasks/{task_id}/actions")
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


@router.post("/admin/moderation/tasks/{task_id}/export")
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


@router.get("/admin/moderation/exports/{export_id}")
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


@router.get("/admin/moderation/stats")
def admin_moderation_stats(
    account_id: Optional[str] = None,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    if admin_user.get("role") not in {"admin", "reviewer", "staff"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin, reviewer or staff role required")
    return {"stats": get_content_moderation_stats(account_id=account_id)}


@router.get("/admin/moderation/policy")
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


@router.put("/admin/moderation/policy")
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
