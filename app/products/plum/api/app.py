"""Plum Feed、会话、模型选择和同步文字 turn API。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.agent_runtime.llm.providers import get_llm_provider
from app.agent_runtime.turns.service import ChannelTurnInput, run_product_turn
from app.bootstrap.product_registry import PLUM_APP_ID
from app.config import settings
from app.db import (
    FixedShellReservationReleased,
    InsufficientWalletBalance,
    SessionPrincipal,
    create_platform_user_session,
    get_platform_user,
    get_duplicate_reply_record,
    get_wallet_summary,
    release_fixed_shell_reservation,
    revoke_platform_user_session,
    reserve_fixed_shells,
)
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, get_channel_capability
from app.products.plum.api.contracts import (
    CreateConversationRequest,
    CreateTurnRequest,
    RedeemAccessCodeRequest,
    UpdateModelRequest,
)
from app.products.plum.api.deps import (
    plum_session_token,
    require_plum_available,
    require_plum_principal,
)
from app.products.plum.application.turn_services import PLUM_TURN_SERVICES
from app.products.plum.infrastructure.repository import (
    create_or_get_conversation,
    redeem_plum_access_invite,
    get_character_experience,
    get_conversation,
    get_entry_account_id,
    get_model_profile,
    list_characters,
    list_conversation_messages,
    list_model_profiles,
    list_user_conversations,
    restart_conversation,
    set_character_favorite,
    set_character_like,
    touch_conversation,
    update_conversation_model,
)
from app.platform.quota.rate_limiter import RateLimiter

router = APIRouter(tags=["plum"])
_auth_rate_limiter = RateLimiter()


def _set_auth_cookies(response: Response, *, session_token: str, csrf_token: str) -> None:
    max_age = max(1, int(settings.plum_session_days)) * 86400
    common = {
        "secure": bool(settings.plum_session_cookie_secure),
        "samesite": "strict",
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(
        key=str(settings.plum_session_cookie_name),
        value=session_token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        key=str(settings.plum_csrf_cookie_name),
        value=csrf_token,
        httponly=False,
        **common,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(str(settings.plum_session_cookie_name), path="/")
    response.delete_cookie(str(settings.plum_csrf_cookie_name), path="/")


@router.post("/auth/access-code")
def redeem_access_code(
    payload: RedeemAccessCodeRequest, request: Request, response: Response
) -> dict:
    if not bool(settings.plum_enabled):
        raise HTTPException(status_code=503, detail="plum_disabled")
    if not bool(settings.plum_public_test_auth_enabled):
        raise HTTPException(status_code=404, detail="public_test_auth_disabled")
    client_host = request.client.host if request.client else "unknown"
    if not _auth_rate_limiter.check_rpm(
        f"plum-auth:{client_host}", 10, window_seconds=60.0
    ):
        raise HTTPException(status_code=429, detail="too_many_login_attempts")
    try:
        identity = redeem_plum_access_invite(
            access_code=payload.access_code,
            display_name=payload.display_name,
        )
        session = create_platform_user_session(
            platform_user_id=str(identity["platform_user_id"]),
            app_id=PLUM_APP_ID,
            days=max(1, int(settings.plum_session_days)),
        )
    except ValueError as err:
        detail = str(err)
        if detail in {"invalid_access_code", "access_code_already_claimed"}:
            detail = "invalid_access_code"
        raise HTTPException(status_code=401, detail=detail) from None
    csrf_token = secrets.token_urlsafe(24)
    _set_auth_cookies(
        response,
        session_token=str(session["token"]),
        csrf_token=csrf_token,
    )
    _no_store(response)
    return {
        "status": "ok",
        "user": {
            "id": identity["platform_user_id"],
            "display_name": identity["display_name"],
        },
        "expires_at": session["expires_at"],
        "wallet": _wallet(str(identity["platform_user_id"])),
    }


@router.get("/auth/me")
def auth_me(
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    user = get_platform_user(platform_user_id=principal.platform_user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="session_expired")
    _no_store(response)
    return {
        "status": "ok",
        "user": {"id": user["id"], "display_name": user.get("display_name")},
        "expires_at": principal.expires_at,
        "wallet": _wallet(principal.platform_user_id),
    }


@router.delete("/auth/session/current")
def logout(
    request: Request,
    response: Response,
    _principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    token = plum_session_token(request)
    if token:
        revoke_platform_user_session(token=token)
    _clear_auth_cookies(response)
    _no_store(response)
    return {"status": "ok"}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _wallet(platform_user_id: str) -> dict:
    entry_account_id = get_entry_account_id(platform_user_id)
    summary = get_wallet_summary(
        account_id=entry_account_id,
        ensure_grant=False,
        create_if_missing=False,
    )
    if not summary:
        raise HTTPException(status_code=503, detail="wallet_not_found")
    wallet = summary["wallet"]
    return {
        "balance": int(wallet["balance_shell_micros"]) // 1_000_000,
        "balance_micros": int(wallet["balance_shell_micros"]),
        "unit": "金币",
    }


def _conversation_or_404(conversation_id: str, principal: SessionPrincipal) -> dict:
    conversation = get_conversation(
        conversation_id=conversation_id,
        platform_user_id=principal.platform_user_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return conversation


@router.get("/bootstrap")
def bootstrap(
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    _no_store(response)
    return {
        "status": "ok",
        "app_id": PLUM_APP_ID,
        "user": {
            "id": principal.platform_user_id,
            "display_name": (
                get_platform_user(platform_user_id=principal.platform_user_id) or {}
            ).get("display_name") or "Plum 测试用户",
        },
        "wallet": _wallet(principal.platform_user_id),
        "models": list_model_profiles(),
    }


@router.get("/feed")
def feed(
    response: Response,
    _available: None = Depends(require_plum_available),
) -> dict:
    """Return the public character catalog; account-scoped state stays private."""

    _no_store(response)
    return {"status": "ok", "items": list_characters()}


def _set_reaction(
    *, character_id: str, principal: SessionPrincipal, reaction: str, active: bool
) -> dict:
    setter = set_character_like if reaction == "like" else set_character_favorite
    try:
        result = setter(
            platform_user_id=principal.platform_user_id,
            character_id=character_id,
            active=active,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    return {"status": "ok", **result}


@router.put("/characters/{character_id}/like")
def like_character(
    character_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    result = _set_reaction(
        character_id=character_id, principal=principal, reaction="like", active=True
    )
    _no_store(response)
    return result


@router.delete("/characters/{character_id}/like")
def unlike_character(
    character_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    result = _set_reaction(
        character_id=character_id, principal=principal, reaction="like", active=False
    )
    _no_store(response)
    return result


@router.put("/characters/{character_id}/favorite")
def favorite_character(
    character_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    result = _set_reaction(
        character_id=character_id,
        principal=principal,
        reaction="favorite",
        active=True,
    )
    _no_store(response)
    return result


@router.delete("/characters/{character_id}/favorite")
def unfavorite_character(
    character_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    result = _set_reaction(
        character_id=character_id,
        principal=principal,
        reaction="favorite",
        active=False,
    )
    _no_store(response)
    return result


@router.post("/conversations")
def create_conversation(
    payload: CreateConversationRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    try:
        conversation = create_or_get_conversation(
            platform_user_id=principal.platform_user_id,
            character_id=payload.character_id,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", "conversation": conversation}


@router.get("/conversations")
def conversation_history(
    response: Response,
    limit: int = Query(default=30, ge=1, le=100),
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    """Return the authenticated user's active character conversations."""

    _no_store(response)
    return {
        "status": "ok",
        "items": list_user_conversations(
            platform_user_id=principal.platform_user_id,
            limit=limit,
        ),
    }


@router.get("/conversations/{conversation_id}")
def conversation_detail(
    conversation_id: str,
    response: Response,
    limit: int = Query(default=100, ge=1, le=100),
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    conversation = _conversation_or_404(conversation_id, principal)
    experience = get_character_experience(
        conversation_id=conversation_id,
        platform_user_id=principal.platform_user_id,
    )
    if experience is None:
        raise HTTPException(status_code=503, detail="character_experience_unavailable")
    _no_store(response)
    return {
        "status": "ok",
        "conversation": conversation,
        "messages": list_conversation_messages(conversation, limit=limit),
        "models": list_model_profiles(),
        "wallet": _wallet(principal.platform_user_id),
        "experience": experience,
    }


@router.patch("/conversations/{conversation_id}/model")
def change_model(
    conversation_id: str,
    payload: UpdateModelRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    try:
        conversation = update_conversation_model(
            conversation_id=conversation_id,
            platform_user_id=principal.platform_user_id,
            model_profile=payload.model_profile,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", "conversation": conversation}


@router.post("/conversations/{conversation_id}/restart")
def restart(
    conversation_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    try:
        conversation = restart_conversation(
            conversation_id=conversation_id,
            platform_user_id=principal.platform_user_id,
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    experience = get_character_experience(
        conversation_id=str(conversation["id"]),
        platform_user_id=principal.platform_user_id,
    )
    if experience is None:
        raise HTTPException(status_code=503, detail="character_experience_unavailable")
    _no_store(response)
    return {
        "status": "ok",
        "conversation": conversation,
        "messages": [],
        "wallet": _wallet(principal.platform_user_id),
        "experience": experience,
    }


@router.post("/conversations/{conversation_id}/turns")
def create_turn(
    conversation_id: str,
    payload: CreateTurnRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_principal),
) -> dict:
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="message_empty")
    conversation = _conversation_or_404(conversation_id, principal)
    model = get_model_profile(str(conversation["model_profile"]))
    if model is None:
        raise HTTPException(status_code=503, detail="model_profile_unavailable")
    try:
        provider = get_llm_provider(str(model["provider_id"]), settings_obj=settings)
    except ValueError as err:
        raise HTTPException(status_code=503, detail="model_provider_unavailable") from err
    if not provider.enabled:
        raise HTTPException(status_code=503, detail="model_provider_unavailable")
    reservation_key = (
        f"plum-turn:{conversation_id}:{payload.idempotency_key.strip()}"
    )
    try:
        reserve_fixed_shells(
            account_id=str(conversation["runtime_account_id"]),
            platform_user_id=principal.platform_user_id,
            amount_shell_micros=int(model["coin_cost_micros"]),
            idempotency_key=reservation_key,
            source_id=conversation_id,
            metadata={
                "model_profile": model["profile"],
                "provider_id": model["provider_id"],
                "config_version": model["config_version"],
            },
        )
    except InsufficientWalletBalance as err:
        raise HTTPException(status_code=402, detail="insufficient_coins") from err
    except FixedShellReservationReleased as err:
        raise HTTPException(
            status_code=409,
            detail="idempotency_key_already_released",
        ) from err

    identity = ResolvedIdentity(
        ai4all_account_id=str(conversation["runtime_account_id"]),
        session_key=f"plum:{conversation_id}",
        channel=CHANNEL_APP,
        channel_account_id=None,
        sender_id=principal.platform_user_id,
        chat_id=conversation_id,
    )
    try:
        turn_response = run_product_turn(
            ChannelTurnInput(
                account_id=str(conversation["runtime_account_id"]),
                app_id=PLUM_APP_ID,
                cap=get_channel_capability(CHANNEL_APP),
                identity=identity,
                message_id=payload.client_message_id.strip(),
                event_id=payload.idempotency_key.strip(),
                message_type="text",
                text=text,
                media=None,
                raw={"transport": "plum_web"},
                sender_name="Plum 测试用户",
                provider_id=str(model["provider_id"]),
                usage_billing_enabled=False,
            ),
            product_services=PLUM_TURN_SERVICES,
        )
    except Exception as err:
        release_fixed_shell_reservation(
            account_id=str(conversation["runtime_account_id"]),
            platform_user_id=principal.platform_user_id,
            reservation_idempotency_key=reservation_key,
        )
        raise HTTPException(status_code=502, detail="generation_failed") from err

    charge_kept = turn_response.status in {"ok", "duplicate"} and bool(
        turn_response.reply
    )
    if not charge_kept:
        release_fixed_shell_reservation(
            account_id=str(conversation["runtime_account_id"]),
            platform_user_id=principal.platform_user_id,
            reservation_idempotency_key=reservation_key,
        )
    else:
        touch_conversation(
            conversation_id=conversation_id,
            platform_user_id=principal.platform_user_id,
        )
    reply_row = get_duplicate_reply_record(
        account_id=str(conversation["runtime_account_id"]),
        reply_to_message_id=payload.client_message_id.strip(),
    )
    _no_store(response)
    return {
        "status": turn_response.status,
        "reply": {
            "message_id": reply_row.get("message_id") if reply_row else None,
            "text": turn_response.reply or "",
        },
        "charged_coins": (
            int(model["coin_cost_micros"]) // 1_000_000 if charge_kept else 0
        ),
        "wallet": _wallet(principal.platform_user_id),
        "deduplicated": turn_response.status == "duplicate",
    }


__all__ = ["router"]
