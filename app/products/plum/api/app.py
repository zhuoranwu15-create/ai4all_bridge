"""Plum Feed、会话、模型选择和同步文字 turn API。"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from urllib.parse import urlsplit
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.agent_runtime.llm.providers import get_llm_provider
from app.agent_runtime.turns.service import (
    CancellationToken,
    ChannelTurnInput,
    run_product_turn,
    run_product_turn_stream,
)
from app.bootstrap.product_registry import PLUM_APP_ID
from app.config import settings
from app.db import (
    FixedShellReservationReleased,
    InsufficientWalletBalance,
    SessionPrincipal,
    ActiveRuntimeTurnError,
    create_runtime_turn_run,
    finish_runtime_turn_run,
    get_platform_user,
    get_duplicate_reply_record,
    get_runtime_turn_run,
    get_runtime_turn_run_by_idempotency,
    get_wallet_summary,
    release_fixed_shell_reservation,
    reclaim_stale_runtime_turn_runs,
    request_runtime_turn_cancel,
    revoke_platform_user_session,
    reserve_fixed_shells,
    runtime_turn_cancel_requested,
)
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, get_channel_capability
from app.products.plum.api.contracts import (
    CreateConversationRequest,
    CreateTurnRequest,
    RedeemAccessCodeRequest,
    RestartConversationRequest,
    UpdateModelRequest,
    UpdateGuestProfileRequest,
)
from app.products.plum.api.deps import (
    GuestPrincipal,
    PlumActorPrincipal,
    optional_plum_actor,
    plum_session_token,
    require_plum_available,
    require_plum_member,
    require_plum_actor,
)
from app.products.plum.application.identity import create_plum_login_session
from app.products.plum.application.turn_services import PLUM_TURN_SERVICES
from app.products.plum.infrastructure.repository import (
    PlumConflictError,
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
from app.products.plum.infrastructure.guest_repository import (
    create_guest_session,
    get_guest_profile,
    get_guest_quota,
    upsert_guest_profile,
)
from app.platform.quota.rate_limiter import RateLimiter

router = APIRouter(tags=["plum"])
_auth_rate_limiter = RateLimiter()
logger = logging.getLogger("ai4all.plum")


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


def _set_guest_cookies(
    response: Response, *, session_token: str, csrf_token: str
) -> None:
    max_age = max(1, int(settings.plum_guest_session_days)) * 86400
    common = {
        "secure": bool(settings.plum_session_cookie_secure),
        "samesite": "strict",
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(
        key=str(settings.plum_guest_session_cookie_name),
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


def _capabilities() -> dict:
    return {
        "guest_chat": bool(settings.plum_guest_chat_enabled),
        "email_auth": bool(settings.plum_email_auth_enabled),
        "google_auth": bool(settings.plum_google_auth_enabled),
        "apple_auth": bool(settings.plum_apple_auth_enabled),
        "invite_auth": bool(settings.plum_public_test_auth_enabled),
    }


def _auth_context(actor: Optional[PlumActorPrincipal]) -> dict:
    base = {"status": "ok", "capabilities": _capabilities()}
    if actor is None:
        return {
            **base,
            "actor": {"kind": "visitor", "user": None, "profile_complete": False},
            "guest_quota": None,
            "wallet": None,
            "session_expires_at": None,
        }
    if isinstance(actor, GuestPrincipal):
        profile = get_guest_profile(platform_user_id=actor.platform_user_id)
        return {
            **base,
            "actor": {
                "kind": "guest",
                "user": {"id": actor.platform_user_id, "display_name": None},
                "profile_complete": profile is not None,
                "profile": profile,
            },
            "guest_quota": get_guest_quota(platform_user_id=actor.platform_user_id),
            "wallet": None,
            "session_expires_at": actor.expires_at,
        }
    user = get_platform_user(platform_user_id=actor.platform_user_id) or {}
    return {
        **base,
        "actor": {
            "kind": "member",
            "user": {"id": actor.platform_user_id, "display_name": user.get("display_name")},
            "profile_complete": True,
        },
        "guest_quota": None,
        "wallet": _wallet(actor.platform_user_id),
        "session_expires_at": actor.expires_at,
    }


def _require_same_origin_guest_create(request: Request) -> None:
    """The CSRF-less Guest bootstrap only accepts same-origin browser requests."""

    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise HTTPException(status_code=403, detail="cross_site_request_rejected")
    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
        raise HTTPException(status_code=403, detail="origin_required")
    requested = urlsplit(origin)
    expected = urlsplit(str(request.base_url))
    if (requested.scheme, requested.netloc) != (expected.scheme, expected.netloc):
        raise HTTPException(status_code=403, detail="cross_site_request_rejected")


@router.get("/auth/context")
def auth_context(
    response: Response,
    actor: Optional[PlumActorPrincipal] = Depends(optional_plum_actor),
) -> dict:
    _no_store(response)
    return _auth_context(actor)


@router.post("/auth/guest/session")
def create_or_restore_guest_session(
    request: Request,
    response: Response,
    actor: Optional[PlumActorPrincipal] = Depends(optional_plum_actor),
) -> dict:
    require_plum_available()
    if not bool(settings.plum_guest_chat_enabled):
        raise HTTPException(status_code=404, detail="guest_chat_disabled")
    _require_same_origin_guest_create(request)
    client_host = request.client.host if request.client else "unknown"
    if not _auth_rate_limiter.check_rpm(
        f"plum-guest-create:{client_host}", 10, window_seconds=60.0
    ):
        raise HTTPException(status_code=429, detail="too_many_guest_sessions")
    if actor is not None:
        _no_store(response)
        return _auth_context(actor)
    created = create_guest_session(
        days=max(1, int(settings.plum_guest_session_days)),
        client_ip=client_host,
        user_agent=request.headers.get("User-Agent"),
    )
    _set_guest_cookies(
        response,
        session_token=str(created["token"]),
        csrf_token=str(created["csrf_token"]),
    )
    _no_store(response)
    return _auth_context(created["principal"])


@router.patch("/auth/guest/profile")
def update_guest_profile(
    payload: UpdateGuestProfileRequest,
    response: Response,
    actor: PlumActorPrincipal = Depends(require_plum_actor),
) -> dict:
    if not isinstance(actor, GuestPrincipal):
        raise HTTPException(status_code=409, detail="guest_profile_not_available")
    if not payload.adult_confirmed:
        raise HTTPException(status_code=403, detail="adult_confirmation_required")
    upsert_guest_profile(
        platform_user_id=actor.platform_user_id,
        pronouns=payload.pronouns,
        relationship_preference=payload.relationship_preference,
        genres=payload.genres,
    )
    _no_store(response)
    return _auth_context(actor)


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
        login = create_plum_login_session(
            verified_platform_user_id=str(identity["platform_user_id"]),
            display_name=str(identity["display_name"]),
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
        session_token=str(login["session"]["token"]),
        csrf_token=csrf_token,
    )
    _no_store(response)
    return {
        "status": "ok",
        "user": {
            "id": identity["platform_user_id"],
            "display_name": identity["display_name"],
        },
        "expires_at": login["session"]["expires_at"],
        "wallet": _wallet(str(identity["platform_user_id"])),
    }


@router.get("/auth/me")
def auth_me(
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
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
    _principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
        "capabilities": {
            "chat_streaming": bool(settings.plum_chat_streaming_enabled),
        },
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    principal: SessionPrincipal = Depends(require_plum_member),
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
    payload: RestartConversationRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    try:
        conversation = restart_conversation(
            conversation_id=conversation_id,
            platform_user_id=principal.platform_user_id,
            creation_idempotency_key=payload.idempotency_key,
        )
    except PlumConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
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
    principal: SessionPrincipal = Depends(require_plum_member),
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


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _release_stream_reservation(
    *, conversation: dict, principal: SessionPrincipal, reservation_key: str
) -> None:
    release_fixed_shell_reservation(
        account_id=str(conversation["runtime_account_id"]),
        platform_user_id=principal.platform_user_id,
        reservation_idempotency_key=reservation_key,
    )


def _reclaim_stale_stream_runs(
    *, conversation: dict, principal: SessionPrincipal
) -> None:
    """Clear stale active guards and refund unseen Plum turns on the next request."""
    reclaimed = reclaim_stale_runtime_turn_runs(
        ttl_seconds=600,
        app_id=PLUM_APP_ID,
        account_id=str(conversation["runtime_account_id"]),
        session_id=int(conversation["runtime_session_id"]),
    )
    for run in reclaimed:
        if run.get("first_delta_at") is not None:
            continue
        try:
            _release_stream_reservation(
                conversation=conversation,
                principal=principal,
                reservation_key=(
                    f"plum-turn:{conversation['id']}:{run['idempotency_key']}"
                ),
            )
        except ValueError:
            # Shared Runtime callers may not have a Plum fixed-price reservation.
            logger.warning(
                "stale Plum turn had no releasable reservation turn_id=%s",
                run["id"],
            )


@router.post("/conversations/{conversation_id}/turns/stream")
def create_turn_stream(
    conversation_id: str,
    payload: CreateTurnRequest,
    principal: SessionPrincipal = Depends(require_plum_member),
):
    if not bool(settings.plum_chat_streaming_enabled):
        raise HTTPException(status_code=503, detail="chat_streaming_disabled")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="message_empty")
    request_id = payload.idempotency_key.strip()
    client_message_id = payload.client_message_id.strip()
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

    account_id = str(conversation["runtime_account_id"])
    _reclaim_stale_stream_runs(conversation=conversation, principal=principal)
    existing_run = get_runtime_turn_run_by_idempotency(
        app_id=PLUM_APP_ID,
        account_id=account_id,
        idempotency_key=request_id,
    )
    if existing_run is not None:
        if existing_run["status"] != "completed":
            raise HTTPException(status_code=409, detail="turn_idempotency_conflict")
        reply_row = get_duplicate_reply_record(
            account_id=account_id,
            reply_to_message_id=client_message_id,
        )
        if reply_row is None:
            raise HTTPException(status_code=409, detail="turn_replay_unavailable")

        def replay():
            yield _sse(
                "turn.accepted",
                {
                    "version": 1,
                    "turn_id": existing_run["id"],
                    "request_id": request_id,
                    "model_profile": model["profile"],
                    "reserved_coins": int(model["coin_cost_micros"]) // 1_000_000,
                    "deduplicated": True,
                },
            )
            yield _sse(
                "message.delta",
                {
                    "version": 1,
                    "turn_id": existing_run["id"],
                    "seq": 1,
                    "text": str(reply_row["content"]),
                },
            )
            yield _sse(
                "turn.completed",
                {
                    "version": 1,
                    "turn_id": existing_run["id"],
                    "message_id": reply_row["message_id"],
                    "finish_reason": existing_run.get("finish_reason") or "stop",
                    "charged_coins": int(model["coin_cost_micros"]) // 1_000_000,
                    "wallet": _wallet(principal.platform_user_id),
                    "deduplicated": True,
                },
            )

        return StreamingResponse(
            replay(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    reservation_key = f"plum-turn:{conversation_id}:{request_id}"
    try:
        reserve_fixed_shells(
            account_id=account_id,
            platform_user_id=principal.platform_user_id,
            amount_shell_micros=int(model["coin_cost_micros"]),
            idempotency_key=reservation_key,
            source_id=conversation_id,
            metadata={
                "model_profile": model["profile"],
                "provider_id": model["provider_id"],
                "config_version": model["config_version"],
                "transport": "sse",
            },
        )
    except InsufficientWalletBalance as err:
        raise HTTPException(status_code=402, detail="insufficient_coins") from err
    except FixedShellReservationReleased as err:
        raise HTTPException(status_code=409, detail="idempotency_key_already_released") from err

    turn_id = f"turn_{uuid.uuid4().hex}"
    try:
        _run, created = create_runtime_turn_run(
            turn_id=turn_id,
            app_id=PLUM_APP_ID,
            account_id=account_id,
            session_id=int(conversation["runtime_session_id"]),
            client_message_id=client_message_id,
            idempotency_key=request_id,
            provider_id=provider.id,
            model_ref=provider.model,
        )
    except ActiveRuntimeTurnError as err:
        _release_stream_reservation(
            conversation=conversation,
            principal=principal,
            reservation_key=reservation_key,
        )
        raise HTTPException(status_code=409, detail="conversation_turn_active") from err
    except Exception as err:
        logger.exception(
            "runtime turn initialization failed turn_id=%s account=%s",
            turn_id,
            account_id,
        )
        finish_runtime_turn_run(
            turn_id=turn_id,
            status="failed",
            error_code="runtime_turn_initialization_failed",
        )
        _release_stream_reservation(
            conversation=conversation,
            principal=principal,
            reservation_key=reservation_key,
        )
        raise HTTPException(status_code=503, detail="runtime_turn_unavailable") from err
    if not created:
        raise HTTPException(status_code=409, detail="turn_idempotency_conflict")

    identity = ResolvedIdentity(
        ai4all_account_id=account_id,
        session_key=f"plum:{conversation_id}",
        channel=CHANNEL_APP,
        channel_account_id=None,
        sender_id=principal.platform_user_id,
        chat_id=conversation_id,
    )
    turn_input = ChannelTurnInput(
        account_id=account_id,
        app_id=PLUM_APP_ID,
        cap=get_channel_capability(CHANNEL_APP),
        identity=identity,
        message_id=client_message_id,
        event_id=request_id,
        message_type="text",
        text=text,
        media=None,
        raw={"transport": "plum_web_sse"},
        sender_name="Plum 测试用户",
        provider_id=str(model["provider_id"]),
        usage_billing_enabled=False,
        turn_id=turn_id,
        client_message_id=client_message_id,
        idempotency_key=request_id,
    )
    cancellation = CancellationToken(
        external_check=lambda: runtime_turn_cancel_requested(turn_id)
    )

    def stream():
        saw_delta = False
        settled = False
        runtime_events = run_product_turn_stream(
            turn_input,
            product_services=PLUM_TURN_SERVICES,
            cancellation=cancellation,
        )
        try:
            for event in runtime_events:
                if event.kind == "accepted":
                    yield _sse(
                        "turn.accepted",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "request_id": request_id,
                            "model_profile": model["profile"],
                            "reserved_coins": int(model["coin_cost_micros"]) // 1_000_000,
                        },
                    )
                elif event.kind == "text_delta":
                    saw_delta = True
                    yield _sse(
                        "message.delta",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "seq": event.seq,
                            "text": event.text,
                        },
                    )
                elif event.kind == "completed":
                    message_id = (
                        (event.response.metadata or {}).get("reply_message_id")
                        if event.response
                        else None
                    )
                    finish_runtime_turn_run(
                        turn_id=turn_id,
                        status="completed",
                        assistant_message_id=message_id,
                        finish_reason="stop",
                    )
                    settled = True
                    touch_conversation(
                        conversation_id=conversation_id,
                        platform_user_id=principal.platform_user_id,
                    )
                    yield _sse(
                        "turn.completed",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "message_id": message_id,
                            "finish_reason": "stop",
                            "charged_coins": int(model["coin_cost_micros"]) // 1_000_000,
                            "wallet": _wallet(principal.platform_user_id),
                            "deduplicated": False,
                        },
                    )
                elif event.kind == "cancelled":
                    message_id = (
                        (event.response.metadata or {}).get("reply_message_id")
                        if event.response
                        else None
                    )
                    finish_runtime_turn_run(
                        turn_id=turn_id,
                        status="cancelled",
                        assistant_message_id=message_id,
                        finish_reason="client_cancelled",
                    )
                    if not saw_delta:
                        _release_stream_reservation(
                            conversation=conversation,
                            principal=principal,
                            reservation_key=reservation_key,
                        )
                    else:
                        touch_conversation(
                            conversation_id=conversation_id,
                            platform_user_id=principal.platform_user_id,
                        )
                    settled = True
                    yield _sse(
                        "turn.cancelled",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "message_id": message_id,
                            "charged_coins": int(model["coin_cost_micros"]) // 1_000_000 if saw_delta else 0,
                            "wallet": _wallet(principal.platform_user_id),
                        },
                    )
                elif event.kind in {"failed", "deduplicated"}:
                    error_code = event.error_code or "runtime_stream_failed"
                    finish_runtime_turn_run(
                        turn_id=turn_id,
                        status="failed",
                        error_code=error_code,
                    )
                    _release_stream_reservation(
                        conversation=conversation,
                        principal=principal,
                        reservation_key=reservation_key,
                    )
                    settled = True
                    yield _sse(
                        "turn.failed",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "code": error_code,
                            "retryable": True,
                            "charged_coins": 0,
                            "wallet": _wallet(principal.platform_user_id),
                        },
                    )
        except Exception as err:
            logger.exception(
                "Plum stream failed turn_id=%s account=%s error=%s",
                turn_id,
                account_id,
                type(err).__name__,
            )
            if not settled:
                finish_runtime_turn_run(
                    turn_id=turn_id,
                    status="failed",
                    error_code="stream_internal_error",
                )
                _release_stream_reservation(
                    conversation=conversation,
                    principal=principal,
                    reservation_key=reservation_key,
                )
                settled = True
            yield _sse(
                "turn.failed",
                {
                    "version": 1,
                    "turn_id": turn_id,
                    "code": "stream_internal_error",
                    "retryable": True,
                    "charged_coins": 0,
                    "wallet": _wallet(principal.platform_user_id),
                },
            )
        finally:
            cancellation.cancel()
            try:
                runtime_events.close()
            except Exception:
                logger.exception(
                    "runtime stream close failed turn_id=%s account=%s",
                    turn_id,
                    account_id,
                )
            if not settled:
                current_run = get_runtime_turn_run(turn_id)
                if current_run is not None and current_run["status"] in {
                    "accepted",
                    "running",
                }:
                    finish_runtime_turn_run(
                        turn_id=turn_id,
                        status="cancelled",
                        finish_reason="client_disconnected",
                    )
                if saw_delta:
                    touch_conversation(
                        conversation_id=conversation_id,
                        platform_user_id=principal.platform_user_id,
                    )
                else:
                    _release_stream_reservation(
                        conversation=conversation,
                        principal=principal,
                        reservation_key=reservation_key,
                    )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/turns/{request_id}/cancel")
def cancel_turn_stream(
    conversation_id: str,
    request_id: str,
    principal: SessionPrincipal = Depends(require_plum_member),
):
    """Persist a cooperative Stop signal before the browser tears down its stream."""
    conversation = _conversation_or_404(conversation_id, principal)
    account_id = str(conversation["runtime_account_id"])
    run = get_runtime_turn_run_by_idempotency(
        app_id=PLUM_APP_ID,
        account_id=account_id,
        idempotency_key=request_id,
    )
    if run is None or int(run["session_id"]) != int(conversation["runtime_session_id"]):
        raise HTTPException(status_code=404, detail="turn_not_found")
    requested = request_runtime_turn_cancel(
        turn_id=str(run["id"]),
        app_id=PLUM_APP_ID,
        account_id=account_id,
        session_id=int(conversation["runtime_session_id"]),
    )
    current = get_runtime_turn_run(str(run["id"])) or run
    return {
        "status": "ok",
        "turn_id": str(run["id"]),
        "cancel_requested": requested or current.get("cancel_requested_at") is not None,
        "run_status": current["status"],
        "wallet": _wallet(principal.platform_user_id),
    }


__all__ = ["router"]
