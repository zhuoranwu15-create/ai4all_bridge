"""Companion World 管理端：M4 lifecycle 脱敏 review queue。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db import (
    get_resident_lifecycle_event,
    insert_admin_access_event,
    transition_resident_lifecycle_event,
)
from app.products.mingchan.application.lifecycle import (
    LifecycleCommitError,
    approve_lifecycle_event,
    correct_lifecycle_event,
    get_lifecycle_review_event,
    list_lifecycle_review_events,
    serialize_lifecycle_event,
)
from app.products.mingchan.application.mailbox import (
    MailboxError,
    create_mailbox_catalog_entry,
    list_mailbox_catalog,
    retire_mailbox_catalog_entry,
)
from app.routers.deps import require_admin_or_staff_user, require_admin_user
from app.routers.serializers import _normalize_ts, _now_db_time
from app.time_utils import beijing_naive_now

router = APIRouter()


class LifecycleReviewDecisionRequest(BaseModel):
    """可逆 review 决策；reason 会进入 append-only audit。"""

    reason: str = Field(min_length=1, max_length=1000)


class LifecycleApproveRequest(BaseModel):
    """full-admin offline 提交；farewell 是事务外已确定的安全终稿。"""

    farewell_text: str
    reason: str = Field(min_length=1, max_length=1000)
    allow_last_resident_exception: bool = False


class LifecycleCorrectionRequest(BaseModel):
    """post-commit 纠错；可隐藏 farewell，但不存在 offline 恢复选项。"""

    reason: str = Field(min_length=1, max_length=1000)
    hide_farewell: bool = False


class MailboxCatalogCreateRequest(BaseModel):
    """创建不可变 catalog entry；不允许夹带 persona/owner/runtime 字段。"""

    model_config = ConfigDict(extra="forbid")

    character_key: str = Field(min_length=1, max_length=128)
    character_template_id: str = Field(min_length=1, max_length=128)
    template_version: str = Field(min_length=1, max_length=128)
    letter_body: str
    priority: int = Field(default=0, ge=-1000, le=1000)
    available_from: Optional[str] = None
    available_until: Optional[str] = None


class MailboxCatalogRetireRequest(BaseModel):
    """retire 审计原因；已投递 letter 快照不受影响。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


def _error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code})


def _commit_error(err: LifecycleCommitError) -> HTTPException:
    http_status = {
        "lifecycle_event_not_found": status.HTTP_404_NOT_FOUND,
        "farewell_invalid": status.HTTP_422_UNPROCESSABLE_ENTITY,
    }.get(err.code, status.HTTP_409_CONFLICT)
    return _error(err.code, http_status)


def _mailbox_error(err: MailboxError) -> HTTPException:
    http_status = (
        status.HTTP_404_NOT_FOUND
        if err.code == "mailbox_catalog_not_found"
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return _error(err.code, http_status)


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


def _normalize_catalog_times(item: dict) -> dict:
    return _normalize_ts(
        item,
        "available_from",
        "available_until",
        "retired_at",
        "created_at",
        "updated_at",
    )


def _audit_catalog(
    *,
    admin_user: dict,
    catalog: dict,
    action: str,
    request: Request,
    reason: Optional[str],
) -> None:
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action=f"companion_world.mailbox_catalog.{action}",
        resource_type="character_letter_catalog",
        resource_id=str(catalog["id"]),
        plaintext=False,
        reason=reason,
        request_path=request.url.path,
        metadata={
            "character_key": catalog.get("character_key"),
            "character_template_id": catalog.get("character_template_id"),
            "template_version": catalog.get("template_version"),
            "status": catalog.get("status"),
        },
    )


@router.get("/admin/products/mingchan/world/mailbox/catalog")
def admin_list_mailbox_catalog(
    response: Response,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    statuses = tuple(
        item.strip()
        for item in (status_filter or "active").split(",")
        if item.strip()
    )
    try:
        rows = list_mailbox_catalog(statuses=statuses, limit=limit)
    except MailboxError as err:
        raise _mailbox_error(err) from err
    response.headers["Cache-Control"] = "no-store"
    return {"entries": [_normalize_catalog_times(row) for row in rows]}


@router.post("/admin/products/mingchan/world/mailbox/catalog")
def admin_create_mailbox_catalog(
    payload: MailboxCatalogCreateRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    try:
        entry, created = create_mailbox_catalog_entry(
            character_key=payload.character_key,
            character_template_id=payload.character_template_id,
            template_version=payload.template_version,
            letter_body=payload.letter_body,
            priority=payload.priority,
            created_by=str(admin_user.get("id") or ""),
            available_from=payload.available_from,
            available_until=payload.available_until,
        )
    except MailboxError as err:
        raise _mailbox_error(err) from err
    _audit_catalog(
        admin_user=admin_user,
        catalog=entry,
        action=("create" if created else "create_replay"),
        request=request,
        reason=None,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["Cache-Control"] = "no-store"
    return {"entry": _normalize_catalog_times(entry), "created": created}


@router.post("/admin/products/mingchan/world/mailbox/catalog/{catalog_id}/retire")
def admin_retire_mailbox_catalog(
    catalog_id: str,
    payload: MailboxCatalogRetireRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_or_staff_user),
) -> dict:
    try:
        entry = retire_mailbox_catalog_entry(
            catalog_id=catalog_id,
            retired_by=str(admin_user.get("id") or ""),
            retired_at=_now_db_time(),
        )
    except MailboxError as err:
        raise _mailbox_error(err) from err
    _audit_catalog(
        admin_user=admin_user,
        catalog=entry,
        action="retire",
        request=request,
        reason=payload.reason,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"entry": _normalize_catalog_times(entry)}


@router.get("/admin/products/mingchan/world/lifecycle-events")
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


@router.get("/admin/products/mingchan/world/lifecycle-events/{event_id}")
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


@router.post("/admin/products/mingchan/world/lifecycle-events/{event_id}/reject")
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


@router.post("/admin/products/mingchan/world/lifecycle-events/{event_id}/cancel")
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


@router.post("/admin/products/mingchan/world/lifecycle-events/{event_id}/approve")
def admin_approve_lifecycle_event(
    event_id: str,
    payload: LifecycleApproveRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    if get_resident_lifecycle_event(event_id=event_id) is None:
        raise _error("lifecycle_event_not_found", status.HTTP_404_NOT_FOUND)
    if not settings.mingchan_lifecycle_commit_enabled:
        raise _error("lifecycle_commit_disabled", status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        result = approve_lifecycle_event(
            event_id=event_id,
            farewell_text=payload.farewell_text,
            reason=payload.reason,
            allow_last_resident_exception=payload.allow_last_resident_exception,
            admin_user_id=str(admin_user.get("id") or ""),
            now=beijing_naive_now(),
        )
    except LifecycleCommitError as err:
        raise _commit_error(err) from err
    _audit(
        admin_user=admin_user,
        event=result["event"],
        action=("approve_replay" if result["replayed"] else "approve"),
        request=request,
        reason=payload.reason,
    )
    response.headers["Cache-Control"] = "no-store"
    result["event"] = _normalize_event_times(result["event"])
    result["farewell_post"] = _normalize_ts(
        result["farewell_post"], "published_at"
    )
    return result


@router.post("/admin/products/mingchan/world/lifecycle-events/{event_id}/correct")
def admin_correct_lifecycle_event(
    event_id: str,
    payload: LifecycleCorrectionRequest,
    request: Request,
    response: Response,
    admin_user: dict = Depends(require_admin_user),
) -> dict:
    try:
        result = correct_lifecycle_event(
            event_id=event_id,
            reason=payload.reason,
            hide_farewell=payload.hide_farewell,
            admin_user_id=str(admin_user.get("id") or ""),
            now=beijing_naive_now(),
        )
    except LifecycleCommitError as err:
        raise _commit_error(err) from err
    _audit(
        admin_user=admin_user,
        event=result["event"],
        action=("hide_farewell" if payload.hide_farewell else "correct"),
        request=request,
        reason=payload.reason,
    )
    response.headers["Cache-Control"] = "no-store"
    result["event"] = _normalize_event_times(result["event"])
    return result


__all__ = ["router"]
