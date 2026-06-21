"""Admin security 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.routers.admin_security.settings"。"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.routers.deps import get_admin_user, require_admin_or_staff_user, require_admin_user, verify_admin_auth
from app.routers.serializers import _audit_plaintext_access, _clean_scope_list, _normalize_optional_state_datetime, _now_db_time, _require_plaintext_access
from app.db import create_admin_plaintext_grant, get_account, get_admin_plaintext_grant, get_debug_trace, get_message_raw, insert_admin_access_event, list_admin_access_events, list_admin_plaintext_grants, list_admin_users, update_admin_plaintext_grant_status
from app.time_utils import beijing_now
from app.user_profiles import ensure_user_profile, read_agent_context, read_user_profile
from datetime import timedelta
from typing import Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


class PlaintextGrantRequest(BaseModel):
    reason: str
    account_scope: list[str] = Field(default_factory=list)
    resource_scope: list[str] = Field(default_factory=list)
    time_scope_start: Optional[str] = None
    time_scope_end: Optional[str] = None


@router.get("/admin/plaintext/messages/{message_db_id}/raw")
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


@router.get("/admin/plaintext/debug-traces/{trace_id}")
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


@router.get("/admin/plaintext/accounts/{account_id}/user-profile")
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
        "content": read_user_profile(account_id),
        "agent_context": context.metadata(),
        "plaintext": True,
    }


@router.get("/admin/access-events")
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


@router.get("/admin/users")
def admin_users(
    limit: int = 100,
    _: dict = Depends(require_admin_user),
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
    return {"admin_users": list_admin_users(limit=limit)}


@router.post("/admin/plaintext-grants")
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


@router.get("/admin/plaintext-grants")
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


@router.post("/admin/plaintext-grants/{grant_id}/approve")
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


@router.post("/admin/plaintext-grants/{grant_id}/reject")
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


@router.post("/admin/plaintext-grants/{grant_id}/revoke")
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
