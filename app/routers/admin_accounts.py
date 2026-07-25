"""Admin accounts 路由（/admin/...）。从 app.main 拆出，函数体逐字保留。
settings 在本模块绑定，测试需 patch "app.routers.admin_accounts.settings"。"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.routers.deps import get_admin_user, require_admin_user, verify_admin_auth
from app.routers.serializers import _binding_intent_for_view, _can_bypass_redaction_for_account, _debug_redaction_payload, _normalize_ts, _platform_user_for_view, _profile_for_view, _trace_for_view
from app.routers.models import ProfileUpdateRequest
from app.db import get_account, get_account_user_meta, get_daily_usage, get_platform_user, get_profile_for_account, get_usage_last_7_days, get_wallet_summary, list_account_owner_bindings_for_account, list_account_user_meta_current, list_accounts, list_binding_intents_for_account, list_channel_bindings_for_account, list_debug_traces, list_referral_relationships, list_sessions_for_account, list_wallet_ledger, release_due_referral_rewards, set_account_status, set_companion_type_manual, update_account, update_profile_for_account
from app.mission_state import build_admin_mission_view
from app.prompts.user_meta_companion_type import COMPANION_TYPE_ENUM
from app.relationship_state import render_relationship_view
from app.time_utils import beijing_now, beijing_now_str
from app.user_profiles import ensure_user_profile, read_agent_context, read_user_profile
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


class AccountUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None
    daily_limit: Optional[int] = None
    rpm_limit: Optional[int] = None


class CompanionTypeManualRequest(BaseModel):
    primary_type: str
    secondary_types: list[str] = []
    confidence: float = 1.0
    expires_at: Optional[str] = None
    reason: Optional[str] = None


def _validate_companion_payload(payload: CompanionTypeManualRequest) -> None:
    if payload.primary_type not in COMPANION_TYPE_ENUM:
        raise HTTPException(status_code=400, detail="invalid primary_type")
    if payload.confidence < 0.0 or payload.confidence > 1.0:
        raise HTTPException(status_code=400, detail="confidence must be between 0 and 1")
    secondary = []
    for item in payload.secondary_types or []:
        if item not in COMPANION_TYPE_ENUM:
            raise HTTPException(status_code=400, detail="invalid secondary_type")
        if item == payload.primary_type:
            raise HTTPException(status_code=400, detail="secondary_types cannot include primary_type")
        if item not in secondary:
            secondary.append(item)
    if len(secondary) > 3:
        raise HTTPException(status_code=400, detail="secondary_types can contain at most 3 items")
    if payload.expires_at:
        try:
            datetime.strptime(payload.expires_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError as err:
            raise HTTPException(
                status_code=400,
                detail="expires_at must be YYYY-MM-DD HH:MM:SS",
            ) from err


@router.get("/admin/me")
def admin_me(admin_user: dict = Depends(get_admin_user)) -> dict:
    return {"admin_user": admin_user}


@router.get("/admin/accounts")
def admin_accounts(_: None = Depends(verify_admin_auth)) -> dict:
    return {"accounts": [_normalize_ts(a) for a in list_accounts()]}


@router.get("/admin/user-meta")
def admin_user_meta(
    q: Optional[str] = None,
    primary_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 500,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    items = list_account_user_meta_current(
        q=q,
        primary_type=primary_type,
        source=source,
        limit=limit,
    )
    return {
        "items": [
            _normalize_ts(
                item,
                "account_created_at",
                "last_active_at",
                "registered_at",
                "companion_type_last_evaluated_at",
                "companion_type_expires_at",
                "last_evaluated_at",
                "meta_created_at",
                "meta_updated_at",
            )
            for item in items
        ],
    }


@router.get("/admin/accounts/{account_id}")
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


@router.get("/admin/accounts/{account_id}/meta")
def admin_account_meta(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    meta = get_account_user_meta(account_id=account_id)
    # 关系状态可读视图：由 DB 四字段即时渲染，不写回 profile_storage。
    # mission：未分配或模板不可解析统一返回 None（agent_mission_and_orchestration_design.md §7）。
    return {
        "meta": meta,
        "relationship_view": render_relationship_view(meta),
        "mission": build_admin_mission_view(account_id=account_id),
    }


@router.patch("/admin/accounts/{account_id}/meta/companion")
def admin_update_account_companion_meta(
    account_id: str,
    payload: CompanionTypeManualRequest,
    _: dict = Depends(require_admin_user),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    _validate_companion_payload(payload)
    set_companion_type_manual(
        account_id=account_id,
        primary_type=payload.primary_type,
        secondary_types=list(dict.fromkeys(payload.secondary_types or [])),
        confidence=payload.confidence,
        expires_at=payload.expires_at,
        reasoning=payload.reason,
        now=beijing_now_str(),
    )
    return {"status": "ok", "meta": get_account_user_meta(account_id=account_id)}


@router.patch("/admin/accounts/{account_id}")
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


@router.post("/admin/accounts/{account_id}/disable")
def admin_disable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="disabled")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@router.post("/admin/accounts/{account_id}/enable")
def admin_enable_account(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    account = set_account_status(account_id=account_id, status="active")
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}


@router.patch("/admin/accounts/{account_id}/profile")
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


@router.get("/admin/accounts/{account_id}/sessions")
def admin_account_sessions(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"sessions": list_sessions_for_account(account_id=account_id, limit=limit)}


@router.get("/admin/accounts/{account_id}/user-profile")
def admin_get_user_profile(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    path = ensure_user_profile(account_id)
    context = read_agent_context(account_id)
    # P2 后 user_profile.md 真相在 account_profile_files，不再读磁盘。
    content = read_user_profile(account_id)
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


@router.get("/admin/accounts/{account_id}/context-files")
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


@router.get("/admin/accounts/{account_id}/usage")
def admin_account_usage(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    # daily_usage 行按北京日期落库（increment_daily_usage 用 beijing_now().date()）；
    # 这里必须同样用北京日期查询，否则宿主机非 UTC+8 时（如 CI 跨零点）会查不到当天行。
    today = beijing_now().date().isoformat()
    return {
        "account_id": account_id,
        "today": {
            "date": today,
            "message_count": get_daily_usage(account_id=account_id, date=today),
        },
        "last_7_days": get_usage_last_7_days(account_id=account_id),
    }


@router.get("/admin/accounts/{account_id}/wallet")
def admin_account_wallet(
    account_id: str,
    limit: int = 20,
    _: None = Depends(verify_admin_auth),
) -> dict:
    account = get_account(account_id=account_id)
    if account is None:
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
        "app_id": account["app_id"],
        "wallet": wallet,
        "ledger": list_wallet_ledger(account_id=account_id, limit=limit) if wallet else [],
        "redacted": True,
    }


@router.get("/admin/referrals")
def admin_referrals(
    limit: int = 50,
    inviter_platform_user_id: Optional[str] = None,
    invitee_platform_user_id: Optional[str] = None,
    app_id: str = ZHAOXI_APP_ID,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    try:
        referrals = list_referral_relationships(
            limit=limit,
            inviter_platform_user_id=inviter_platform_user_id,
            invitee_platform_user_id=invitee_platform_user_id,
            app_id=app_id,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "referrals": referrals,
        "redacted": True,
    }


@router.post("/admin/referrals/release-due-rewards")
def admin_release_due_referral_rewards(
    limit: int = 200,
    app_id: str = ZHAOXI_APP_ID,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
    try:
        ledgers = release_due_referral_rewards(limit=limit, app_id=app_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "released_count": len(ledgers),
        "ledger": ledgers,
        "redacted": True,
    }
