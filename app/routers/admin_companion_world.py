"""Companion World 管理端：M4 lifecycle 脱敏 review queue。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    get_resident_lifecycle_event,
    insert_admin_access_event,
    transition_resident_lifecycle_event,
)
from app.platform.companion_world_lifecycle import (
    get_lifecycle_review_event,
    list_lifecycle_review_events,
    serialize_lifecycle_event,
)
from app.routers.deps import require_admin_or_staff_user, require_admin_user
from app.routers.serializers import _normalize_ts, _now_db_time

router = APIRouter()


class LifecycleReviewDecisionRequest(BaseModel):
    """可逆 review 决策；reason 会进入 append-only audit。"""

    reason: str = Field(min_length=1, max_length=1000)


class LifecycleApproveRequest(BaseModel):
    """M4-3 approve 预留契约；M4-2 始终不执行不可逆提交。"""

    farewell_text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)
    allow_last_resident_exception: bool = False


def _error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code})


def _audit(
    *, admin_user: dict, event: dict, action: str, request: Request, reason: str | None
) -> None:
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action=f"companion_world.lifecycle.{action}",
        resource_type="resident_lifecycle_event",
        resource_id=str(event["id"]),
        plaintext=False,
        reason=reason,
        request_path=request.url.path,
        metadata={
            "universe_id": event.get("universe_id"),
            "resident_id": event.get("resident_id"),
            "event_type": event.get("event_type"),
            "status": event.get("status"),
        },
    )


def _normalize_event_times(item: dict) -> dict:
    return _normalize_ts(
        item,
        "evidence_window_start",
        "evidence_window_end",
        "cooldown_until",
        "crisis_freeze_until",
        "reviewed_at",
        "committed_at",
        "corrected_at",
        "created_at",
        "updated_at",
    )


@router.get("/admin/companion-world/lifecycle-events")
def admin_list_lifecycle_events(
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = 100,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    statuses = tuple(
        item.strip()
        for item in (status_filter or "review_pending").split(",")
        if item.strip()
    )
    try:
        events = list_lifecycle_review_events(statuses=statuses, limit=limit)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    response.headers["Cache-Control"] = "no-store"
    return {
        "events": [_normalize_event_times(item) for item in events],
        "redacted": True,
    }


@router.get("/admin/companion-world/lifecycle-events/{event_id}")
def admin_lifecycle_event_detail(
    event_id: str,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    event = get_lifecycle_review_event(event_id)
    if event is None:
        raise _error("lifecycle_event_not_found", status.HTTP_404_NOT_FOUND)
    _audit(
        admin_user=admin_user,
        event=event,
        action="view",
        request=request,
        reason=None,
    )
    response.headers["Cache-Control"] = "no-store"
    event["actions"] = [
        _normalize_ts(dict(action), "created_at")
        for action in event.get("actions") or []
    ]
    return {"event": _normalize_event_times(event), "redacted": True}


def _transition_review_event(
    *,
    event_id: str,
    target_status: str,
    action: str,
    payload: LifecycleReviewDecisionRequest,
    request: Request,
    admin_user: dict,
) -> dict:
    event = get_resident_lifecycle_event(event_id=event_id)
    if event is None:
        raise _error("lifecycle_event_not_found", status.HTTP_404_NOT_FOUND)
    current_status = str(event.get("status") or "")
    if current_status not in {"cooling_down", "review_pending"}:
        raise _error("lifecycle_event_not_reviewable", status.HTTP_409_CONFLICT)
    updated = transition_resident_lifecycle_event(
        event_id=event_id,
        expected_status=current_status,
        new_status=target_status,
        action=action,
        actor_type="admin",
        actor_id=str(admin_user.get("id") or ""),
        now=_now_db_time(),
        terminal_reason=payload.reason,
    )
    if updated is None:
        raise _error("lifecycle_event_not_reviewable", status.HTTP_409_CONFLICT)
    _audit(
        admin_user=admin_user,
        event=updated,
        action=target_status,
        request=request,
        reason=payload.reason,
    )
    return {"event": _normalize_event_times(serialize_lifecycle_event(updated))}


@router.post("/admin/companion-world/lifecycle-events/{event_id}/reject")
def admin_reject_lifecycle_event(
    event_id: str,
    payload: LifecycleReviewDecisionRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _transition_review_event(
        event_id=event_id,
        target_status="rejected",
        action="admin_rejected",
        payload=payload,
        request=request,
        admin_user=admin_user,
    )


@router.post("/admin/companion-world/lifecycle-events/{event_id}/cancel")
def admin_cancel_lifecycle_event(
    event_id: str,
    payload: LifecycleReviewDecisionRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _transition_review_event(
        event_id=event_id,
        target_status="cancelled",
        action="admin_cancelled",
        payload=payload,
        request=request,
        admin_user=admin_user,
    )


@router.post("/admin/companion-world/lifecycle-events/{event_id}/approve")
def admin_approve_lifecycle_event(
    event_id: str,
    payload: LifecycleApproveRequest,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    if get_resident_lifecycle_event(event_id=event_id) is None:
        raise _error("lifecycle_event_not_found", status.HTTP_404_NOT_FOUND)
    if not settings.companion_world_lifecycle_commit_enabled:
        raise _error("lifecycle_commit_disabled", status.HTTP_503_SERVICE_UNAVAILABLE)
    # M4-2 即使误开 flag 也不得产生部分提交；完整事务只在 M4-3 接入。
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "lifecycle_commit_unavailable"},
    )


__all__ = ["router"]
