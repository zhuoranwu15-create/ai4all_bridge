"""Plum Feed、会话、模型选择和同步文字 turn API。"""
from __future__ import annotations

import json
import hmac
import logging
import secrets
import uuid
from urllib.parse import urlsplit
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse

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
    CreateCharacterRequest,
    CreateConversationRequest,
    CreateEmailChallengeRequest,
    CreateTurnRequest,
    RedeemAccessCodeRequest,
    RestartConversationRequest,
    UpdateModelRequest,
    UpdateGuestProfileRequest,
    VerifyEmailChallengeRequest,
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
from app.products.plum.application.character_moderation import (
    CharacterModerationUnavailable,
    character_moderation_plugin,
)
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
    list_creator_characters,
    list_creator_tags,
    list_conversation_messages,
    list_model_profiles,
    list_user_conversations,
    restart_conversation,
    publish_created_character,
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
    finish_guest_action,
    reserve_guest_action,
)
from app.products.plum.infrastructure.email_sender import (
    PlumEmailDeliveryUnavailable,
    send_login_code,
)
from app.products.plum.infrastructure.identity_repository import (
    abandon_email_challenge,
    create_email_challenge,
    finalize_email_merge,
    finalize_email_promotion,
    finalize_google_promotion,
    merge_guest_into_email_member,
    normalize_email,
    promote_guest_with_email,
    resolve_email_identity,
    verify_email_challenge,
    create_google_oauth_challenge,
    get_google_oauth_challenge,
    promote_guest_with_google,
    resolve_external_identity,
)
from app.products.plum.infrastructure.google_oauth import (
    GoogleOAuthError,
    authorization_url,
    create_pkce_pair,
    exchange_code,
    verify_token_response,
)
from app.platform.quota.rate_limiter import RateLimiter

router = APIRouter(tags=["plum"])
_auth_rate_limiter = RateLimiter()
logger = logging.getLogger("ai4all.plum")

_GUEST_CONTINUE_PROMPT = (
    "Continue the scene naturally from the character's perspective. "
    "Do not ask the user to supply a hidden prompt."
)


def _guest_continue_prompt(*, character_action_sequence: Optional[int]) -> str:
    if character_action_sequence == 2:
        return (
            f"{_GUEST_CONTINUE_PROMPT} Advance the scene and naturally ask the user "
            "one concrete question they can answer."
        )
    return _GUEST_CONTINUE_PROMPT


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


def _safe_return_to(value: str) -> str:
    candidate = str(value or "/").strip()
    parsed = urlsplit(candidate)
    if not candidate.startswith("/") or candidate.startswith("//") or parsed.scheme or parsed.netloc:
        raise HTTPException(status_code=400, detail="invalid_return_to")
    return candidate


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
        "chat_streaming": bool(settings.plum_chat_streaming_enabled),
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
    if (requested.scheme, requested.netloc) == (expected.scheme, expected.netloc):
        return
    configured = {
        str(item).strip().rstrip("/")
        for item in str(getattr(settings, "plum_web_origins", "") or "").split(",")
        if str(item).strip()
    }
    if origin.rstrip("/") in configured:
        return
    if str(getattr(settings, "app_env", "")) in {"local", "test"} and requested.hostname in {"localhost", "127.0.0.1"}:
        return
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


@router.get("/auth/oauth/google/start")
def start_google_oauth(
    request: Request,
    actor: PlumActorPrincipal = Depends(require_plum_actor),
    return_to: str = Query(default="/", max_length=500),
) -> RedirectResponse:
    require_plum_available()
    if not bool(settings.plum_google_auth_enabled):
        raise HTTPException(status_code=404, detail="google_auth_disabled")
    if not isinstance(actor, GuestPrincipal):
        raise HTTPException(status_code=409, detail="guest_session_required")
    safe_return = _safe_return_to(return_to)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = create_pkce_pair()
    try:
        create_google_oauth_challenge(
            guest_platform_user_id=actor.platform_user_id,
            return_to=safe_return,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            code_challenge=challenge,
        )
        location = authorization_url(state=state, nonce=nonce, code_challenge=challenge)
    except (ValueError, GoogleOAuthError) as err:
        raise HTTPException(status_code=503, detail=str(err)) from None
    response = RedirectResponse(url=location, status_code=307)
    response.set_cookie(
        key="plum_google_oauth", value=json.dumps({"state": state, "nonce": nonce, "verifier": verifier}),
        httponly=True, secure=bool(settings.plum_session_cookie_secure), samesite="lax", path="/",
        max_age=max(60, int(settings.plum_oauth_state_ttl_seconds)),
    )
    return response


@router.get("/auth/oauth/google/callback")
def google_oauth_callback(
    request: Request,
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    require_plum_available()
    if not bool(settings.plum_google_auth_enabled):
        raise HTTPException(status_code=404, detail="google_auth_disabled")
    raw = request.cookies.get("plum_google_oauth")
    try:
        binding = json.loads(raw or "{}")
        if not hmac.compare_digest(str(binding.get("state", "")), state):
            raise GoogleOAuthError("oauth_state_invalid")
        challenge = get_google_oauth_challenge(state=state)
        if str(challenge.get("nonce")) != str(binding.get("nonce")):
            raise GoogleOAuthError("oauth_nonce_invalid")
        expected_binding = hmac.new(
            str(settings.plum_oauth_state_pepper or settings.plum_email_otp_pepper).encode("utf-8"),
            f"nonce:{binding.get('nonce')}:{binding.get('verifier')}".encode("utf-8"),
            "sha256",
        ).hexdigest()
        if not hmac.compare_digest(str(challenge.get("secret_hash")), expected_binding):
            raise GoogleOAuthError("oauth_pkce_invalid")
        token_payload = exchange_code(code=code, code_verifier=str(binding.get("verifier", "")))
        identity = verify_token_response(token_payload, nonce=str(binding["nonce"]))
        guest_id = str(challenge.get("guest_platform_user_id") or "")
        if not guest_id:
            raise GoogleOAuthError("oauth_actor_invalid")
        existing = resolve_external_identity(provider="google", provider_subject=identity.subject)
        if existing is None or str(existing["platform_user_id"]) == guest_id:
            completed = promote_guest_with_google(
                challenge_id=str(challenge["challenge_id"]), guest_platform_user_id=guest_id,
                provider_subject=identity.subject, normalized_email=identity.email,
                display_name=identity.display_name, profile=identity.profile,
            )
            login = create_plum_login_session(verified_platform_user_id=guest_id, display_name=identity.display_name, days=max(1, int(settings.plum_session_days)))
            finalize_google_promotion(challenge_id=str(challenge["challenge_id"]), guest_platform_user_id=guest_id)
            member_id = guest_id
        else:
            target_id = str(existing["platform_user_id"])
            if str(existing["user_status"]) != "active" or str(existing["subject_kind"]) != "member" or str(existing["membership_status"] or "") != "active":
                raise GoogleOAuthError("identity_target_disabled")
            completed = merge_guest_into_email_member(
                challenge_id=str(challenge["challenge_id"]), guest_platform_user_id=guest_id,
                target_platform_user_id=target_id, normalized_email=identity.email,
                provider="google", provider_subject=identity.subject,
            )
            login = create_plum_login_session(verified_platform_user_id=target_id, display_name=str(existing.get("display_name") or identity.display_name), days=max(1, int(settings.plum_session_days)))
            finalize_email_merge(challenge_id=str(challenge["challenge_id"]), guest_platform_user_id=guest_id, merge_run_id=str(completed["merge_run_id"]))
            member_id = target_id
    except (ValueError, GoogleOAuthError, KeyError, json.JSONDecodeError) as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    response = RedirectResponse(url=str(challenge.get("return_to") or "/") + ("&" if "?" in str(challenge.get("return_to") or "/") else "?") + "auth=google_success", status_code=303)
    _set_auth_cookies(response, session_token=str(login["session"]["token"]), csrf_token=secrets.token_urlsafe(24))
    response.delete_cookie("plum_google_oauth", path="/")
    response.delete_cookie(str(settings.plum_guest_session_cookie_name), path="/")
    return response


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


@router.post("/auth/email/challenges", status_code=202)
def create_email_login_challenge(
    payload: CreateEmailChallengeRequest,
    request: Request,
    response: Response,
    actor: PlumActorPrincipal = Depends(require_plum_actor),
) -> dict:
    require_plum_available()
    if not bool(settings.plum_email_auth_enabled):
        raise HTTPException(status_code=404, detail="email_auth_disabled")
    if not isinstance(actor, GuestPrincipal):
        raise HTTPException(status_code=409, detail="guest_session_required")
    client_host = request.client.host if request.client else "unknown"
    if not _auth_rate_limiter.check_rpm(
        f"plum-email-challenge:{client_host}", 10, window_seconds=60.0
    ):
        raise HTTPException(status_code=429, detail="too_many_login_attempts")
    try:
        email = normalize_email(payload.email)
        created = create_email_challenge(
            normalized_email=email,
            guest_platform_user_id=actor.platform_user_id,
        )
        send_login_code(
            email=email,
            code=str(created["code"]),
            expires_minutes=int(created["expires_minutes"]),
        )
    except PlumEmailDeliveryUnavailable as err:
        if "created" in locals():
            abandon_email_challenge(challenge_id=str(created["challenge_id"]))
        raise HTTPException(status_code=503, detail="email_provider_unavailable") from err
    except ValueError as err:
        detail = str(err)
        if detail == "email_challenge_too_frequent":
            raise HTTPException(status_code=429, detail=detail) from None
        if detail == "email_auth_not_configured":
            raise HTTPException(status_code=503, detail=detail) from None
        raise HTTPException(status_code=422, detail=detail) from None
    _no_store(response)
    return {
        "status": "accepted",
        "challenge_id": created["challenge_id"],
        "retry_after_seconds": max(1, int(settings.plum_email_otp_resend_seconds)),
    }


@router.post("/auth/email/verify")
def verify_email_login_challenge(
    payload: VerifyEmailChallengeRequest,
    response: Response,
    actor: PlumActorPrincipal = Depends(require_plum_actor),
) -> dict:
    require_plum_available()
    if not bool(settings.plum_email_auth_enabled):
        raise HTTPException(status_code=404, detail="email_auth_disabled")
    if not isinstance(actor, GuestPrincipal):
        # Visitor account creation and returning-identity login are delivered
        # with the merge/returning-member capability block.
        raise HTTPException(status_code=409, detail="guest_session_required")
    try:
        verified = verify_email_challenge(
            challenge_id=payload.challenge_id,
            code=payload.code,
            guest_platform_user_id=actor.platform_user_id,
        )
        existing = resolve_email_identity(
            normalized_email=str(verified["normalized_email"])
        )
        if existing is None or str(existing["platform_user_id"]) == actor.platform_user_id:
            completed = promote_guest_with_email(
                challenge_id=payload.challenge_id,
                guest_platform_user_id=actor.platform_user_id,
                normalized_email=str(verified["normalized_email"]),
                preferred_name=payload.preferred_name,
            )
            login = create_plum_login_session(
                verified_platform_user_id=actor.platform_user_id,
                display_name=str(completed["display_name"]),
                days=max(1, int(settings.plum_session_days)),
            )
            finalize_email_promotion(
                challenge_id=payload.challenge_id,
                guest_platform_user_id=actor.platform_user_id,
            )
            member_id = actor.platform_user_id
        else:
            target_id = str(existing["platform_user_id"])
            if (
                str(existing["user_status"]) != "active"
                or str(existing["subject_kind"]) != "member"
                or str(existing["membership_status"] or "") != "active"
            ):
                raise ValueError("identity_target_disabled")
            completed = merge_guest_into_email_member(
                challenge_id=payload.challenge_id,
                guest_platform_user_id=actor.platform_user_id,
                target_platform_user_id=target_id,
                normalized_email=str(verified["normalized_email"]),
            )
            login = create_plum_login_session(
                verified_platform_user_id=target_id,
                display_name=str(existing["display_name"] or "Plum User"),
                days=max(1, int(settings.plum_session_days)),
            )
            finalize_email_merge(
                challenge_id=payload.challenge_id,
                guest_platform_user_id=actor.platform_user_id,
                merge_run_id=str(completed["merge_run_id"]),
            )
            member_id = target_id
    except ValueError as err:
        detail = str(err)
        status_code = 409 if detail in {
            "email_challenge_consumed",
            "email_challenge_actor_mismatch",
            "email_identity_merge_required",
            "guest_not_promotable",
            "guest_not_mergeable",
            "identity_merge_conflict",
            "identity_target_disabled",
        } else 400
        raise HTTPException(status_code=status_code, detail=detail) from None
    csrf_token = secrets.token_urlsafe(24)
    _set_auth_cookies(
        response,
        session_token=str(login["session"]["token"]),
        csrf_token=csrf_token,
    )
    response.delete_cookie(str(settings.plum_guest_session_cookie_name), path="/")
    _no_store(response)
    is_new_membership = bool(completed.get("is_new_membership", False))
    return {
        "status": "ok",
        "actor": {
            "kind": "member",
            "user": {
                "id": member_id,
                "display_name": login["platform_user"]["display_name"],
            },
        },
        "is_new_membership": is_new_membership,
        "merge": completed["merge"],
        "grant": {"amount": 1000, "was_applied": is_new_membership},
        "wallet": _wallet(member_id),
        "expires_at": login["session"]["expires_at"],
    }


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


def _conversation_or_404(conversation_id: str, principal: PlumActorPrincipal) -> dict:
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


@router.get("/creator/characters")
def creator_characters(
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Return owner-scoped Character summaries for My Studio."""

    _no_store(response)
    return {
        "status": "ok",
        "items": list_creator_characters(
            platform_user_id=principal.platform_user_id
        ),
    }


@router.get("/creator/tags")
def creator_tags(
    response: Response,
    _principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Return the active controlled Tag vocabulary accepted by create."""

    _no_store(response)
    return {"status": "ok", "items": list_creator_tags()}


@router.post("/creator/characters")
def create_creator_character(
    payload: CreateCharacterRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Validate, review and atomically publish one owner-scoped Character."""

    create_input = payload.model_dump(
        exclude={"adult_confirmed", "rights_confirmed"}
    )
    try:
        decision = character_moderation_plugin(app_env=settings.app_env).review(
            platform_user_id=principal.platform_user_id,
            normalized_payload=create_input,
            creator_declared_rating=payload.creator_declared_rating,
        )
    except CharacterModerationUnavailable as err:
        raise HTTPException(
            status_code=503, detail="character_moderation_unavailable"
        ) from err
    if decision.status != "approved":
        raise HTTPException(status_code=422, detail="character_moderation_rejected")
    try:
        created = publish_created_character(
            platform_user_id=principal.platform_user_id,
            approved_moderation_decision_id=decision.decision_id,
            platform_effective_rating=decision.effective_rating,
            **create_input,
        )
    except PlumConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except ValueError as err:
        detail = str(err)
        if detail == "creator_media_not_claimable":
            raise HTTPException(status_code=409, detail=detail) from err
        if detail == "character_tag_invalid":
            raise HTTPException(status_code=422, detail=detail) from err
        raise HTTPException(status_code=422, detail="character_payload_invalid") from err
    _no_store(response)
    return {"status": "ok", "character": created}


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
    principal: PlumActorPrincipal = Depends(require_plum_actor),
) -> dict:
    if isinstance(principal, GuestPrincipal) and get_guest_profile(
        platform_user_id=principal.platform_user_id
    ) is None:
        raise HTTPException(status_code=403, detail="guest_profile_required")
    try:
        conversation = create_or_get_conversation(
            platform_user_id=principal.platform_user_id,
            character_id=payload.character_id,
            model_profile=("guest_free" if isinstance(principal, GuestPrincipal) else None),
        )
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", "conversation": conversation}


@router.get("/conversations")
def conversation_history(
    response: Response,
    limit: int = Query(default=30, ge=1, le=100),
    principal: PlumActorPrincipal = Depends(require_plum_actor),
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
    principal: PlumActorPrincipal = Depends(require_plum_actor),
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
        "models": (
            [] if isinstance(principal, GuestPrincipal) else list_model_profiles()
        ),
        "wallet": (
            None
            if isinstance(principal, GuestPrincipal)
            else _wallet(principal.platform_user_id)
        ),
        "guest_quota": (
            get_guest_quota(platform_user_id=principal.platform_user_id)
            if isinstance(principal, GuestPrincipal)
            else None
        ),
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
    principal: PlumActorPrincipal = Depends(require_plum_actor),
) -> dict:
    if isinstance(principal, GuestPrincipal) and payload.action is None:
        raise HTTPException(status_code=422, detail="guest_action_required")
    action_kind = payload.action.kind if payload.action else "message"
    text = str(payload.action.text if payload.action else payload.text or "").strip()
    if not text:
        if action_kind != "continue":
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
    guest_finish_required = False
    character_action_sequence = None
    if isinstance(principal, GuestPrincipal):
        if str(conversation["model_profile"]) != "guest_free":
            raise HTTPException(status_code=403, detail="guest_model_restricted")
        try:
            reservation = reserve_guest_action(
                platform_user_id=principal.platform_user_id,
                character_id=str(conversation["character_id"]),
                conversation_id=conversation_id,
                client_action_id=payload.idempotency_key.strip(),
                action_kind=action_kind,
            )
        except ValueError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        if reservation.get("reason"):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "guest_sign_in_required",
                    "reason": str(reservation["reason"]),
                },
            )
        if reservation.get("in_progress"):
            raise HTTPException(status_code=409, detail="turn_idempotency_conflict")
        if reservation.get("completed"):
            reply_row = get_duplicate_reply_record(
                account_id=str(conversation["runtime_account_id"]),
                reply_to_message_id=payload.client_message_id.strip(),
            )
            if reply_row is None:
                raise HTTPException(status_code=409, detail="turn_replay_unavailable")
            _no_store(response)
            return {
                "status": "duplicate",
                "reply": {
                    "message_id": reply_row["message_id"],
                    "text": str(reply_row["content"]),
                },
                "charged_coins": 0,
                "wallet": None,
                "guest_quota": get_guest_quota(
                    platform_user_id=principal.platform_user_id
                ),
                "deduplicated": True,
            }
        guest_finish_required = bool(reservation.get("finish_required"))
        character_action_sequence = reservation.get("character_action_sequence")
    if action_kind == "continue":
        text = _guest_continue_prompt(
            character_action_sequence=character_action_sequence
        )
    reservation_key = (
        f"plum-turn:{conversation_id}:{payload.idempotency_key.strip()}"
    )
    try:
        if not isinstance(principal, GuestPrincipal):
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
                max_output_tokens=(
                    int(settings.plum_guest_max_output_tokens)
                    if isinstance(principal, GuestPrincipal)
                    else None
                ),
                persist_inbound_message=action_kind != "continue",
            ),
            product_services=PLUM_TURN_SERVICES,
        )
    except Exception as err:
        logger.exception(
            "Plum turn generation failed conversation=%s actor=%s",
            conversation_id,
            principal.platform_user_id,
        )
        if not isinstance(principal, GuestPrincipal):
            release_fixed_shell_reservation(
                account_id=str(conversation["runtime_account_id"]),
                platform_user_id=principal.platform_user_id,
                reservation_idempotency_key=reservation_key,
            )
        if guest_finish_required:
            finish_guest_action(
                platform_user_id=principal.platform_user_id,
                client_action_id=payload.idempotency_key.strip(),
                success=False,
            )
        raise HTTPException(status_code=502, detail="generation_failed") from err

    charge_kept = turn_response.status in {"ok", "duplicate"} and bool(
        turn_response.reply
    )
    if not charge_kept and not isinstance(principal, GuestPrincipal):
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
    if isinstance(principal, GuestPrincipal) and guest_finish_required:
        finish_guest_action(
            platform_user_id=principal.platform_user_id,
            client_action_id=payload.idempotency_key.strip(),
            success=charge_kept,
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
        "wallet": (
            None
            if isinstance(principal, GuestPrincipal)
            else _wallet(principal.platform_user_id)
        ),
        "guest_quota": (
            get_guest_quota(platform_user_id=principal.platform_user_id)
            if isinstance(principal, GuestPrincipal)
            else None
        ),
        "deduplicated": turn_response.status == "duplicate",
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _release_stream_reservation(
    *, conversation: dict, principal: PlumActorPrincipal, reservation_key: str
) -> None:
    if isinstance(principal, GuestPrincipal):
        return
    release_fixed_shell_reservation(
        account_id=str(conversation["runtime_account_id"]),
        platform_user_id=principal.platform_user_id,
        reservation_idempotency_key=reservation_key,
    )


def _reclaim_stale_stream_runs(
    *, conversation: dict, principal: PlumActorPrincipal
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
    principal: PlumActorPrincipal = Depends(require_plum_actor),
):
    if not bool(settings.plum_chat_streaming_enabled):
        raise HTTPException(status_code=503, detail="chat_streaming_disabled")
    if isinstance(principal, GuestPrincipal) and payload.action is None:
        raise HTTPException(status_code=422, detail="guest_action_required")
    action_kind = payload.action.kind if payload.action else "message"
    text = str(payload.action.text if payload.action else payload.text or "").strip()
    if not text:
        if action_kind != "continue":
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
    guest_finish_required = False
    guest_recovery_required = False
    character_action_sequence = None
    if isinstance(principal, GuestPrincipal):
        if str(conversation["model_profile"]) != "guest_free":
            raise HTTPException(status_code=403, detail="guest_model_restricted")
        try:
            reservation = reserve_guest_action(
                platform_user_id=principal.platform_user_id,
                character_id=str(conversation["character_id"]),
                conversation_id=conversation_id,
                client_action_id=request_id,
                action_kind=action_kind,
            )
        except ValueError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        if reservation.get("reason"):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "guest_sign_in_required",
                    "reason": str(reservation["reason"]),
                },
            )
        guest_recovery_required = bool(
            reservation.get("in_progress") or reservation.get("completed")
        )
        guest_finish_required = bool(reservation.get("finish_required"))
        character_action_sequence = reservation.get("character_action_sequence")
    if action_kind == "continue":
        text = _guest_continue_prompt(
            character_action_sequence=character_action_sequence
        )

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
            if isinstance(principal, GuestPrincipal) and guest_finish_required:
                finish_guest_action(
                    platform_user_id=principal.platform_user_id,
                    client_action_id=request_id,
                    success=False,
                )
            raise HTTPException(status_code=409, detail="turn_replay_unavailable")
        if isinstance(principal, GuestPrincipal) and guest_finish_required:
            finish_guest_action(
                platform_user_id=principal.platform_user_id,
                client_action_id=request_id,
                success=True,
            )

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
                    "wallet": (
                        None if isinstance(principal, GuestPrincipal)
                        else _wallet(principal.platform_user_id)
                    ),
                    "guest_quota": (
                        get_guest_quota(platform_user_id=principal.platform_user_id)
                        if isinstance(principal, GuestPrincipal) else None
                    ),
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

    if isinstance(principal, GuestPrincipal) and guest_recovery_required:
        # A receipt without a Runtime run means the previous request stopped
        # between quota reservation and run creation. Reuse the same quota.
        guest_finish_required = True

    reservation_key = f"plum-turn:{conversation_id}:{request_id}"
    try:
        if not isinstance(principal, GuestPrincipal):
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
        if isinstance(principal, GuestPrincipal) and guest_finish_required:
            finish_guest_action(
                platform_user_id=principal.platform_user_id,
                client_action_id=request_id,
                success=False,
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
        if isinstance(principal, GuestPrincipal) and guest_finish_required:
            finish_guest_action(
                platform_user_id=principal.platform_user_id,
                client_action_id=request_id,
                success=False,
            )
        raise HTTPException(status_code=503, detail="runtime_turn_unavailable") from err
    if not created:
        if isinstance(principal, GuestPrincipal) and guest_finish_required:
            finish_guest_action(
                platform_user_id=principal.platform_user_id,
                client_action_id=request_id,
                success=False,
            )
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
        max_output_tokens=(
            int(settings.plum_guest_max_output_tokens)
            if isinstance(principal, GuestPrincipal) else None
        ),
        persist_inbound_message=action_kind != "continue",
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
                            "guest_quota": (
                                get_guest_quota(platform_user_id=principal.platform_user_id)
                                if isinstance(principal, GuestPrincipal) else None
                            ),
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
                    if isinstance(principal, GuestPrincipal) and guest_finish_required:
                        finish_guest_action(
                            platform_user_id=principal.platform_user_id,
                            client_action_id=request_id,
                            success=True,
                        )
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
                            "wallet": (
                                None if isinstance(principal, GuestPrincipal)
                                else _wallet(principal.platform_user_id)
                            ),
                            "guest_quota": (
                                get_guest_quota(platform_user_id=principal.platform_user_id)
                                if isinstance(principal, GuestPrincipal) else None
                            ),
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
                    if isinstance(principal, GuestPrincipal) and guest_finish_required:
                        finish_guest_action(
                            platform_user_id=principal.platform_user_id,
                            client_action_id=request_id,
                            success=saw_delta,
                        )
                    yield _sse(
                        "turn.cancelled",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "message_id": message_id,
                            "charged_coins": int(model["coin_cost_micros"]) // 1_000_000 if saw_delta else 0,
                            "wallet": (
                                None if isinstance(principal, GuestPrincipal)
                                else _wallet(principal.platform_user_id)
                            ),
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
                    if isinstance(principal, GuestPrincipal) and guest_finish_required:
                        finish_guest_action(
                            platform_user_id=principal.platform_user_id,
                            client_action_id=request_id,
                            success=False,
                        )
                    yield _sse(
                        "turn.failed",
                        {
                            "version": 1,
                            "turn_id": turn_id,
                            "code": error_code,
                            "retryable": True,
                            "charged_coins": 0,
                            "wallet": (
                                None if isinstance(principal, GuestPrincipal)
                                else _wallet(principal.platform_user_id)
                            ),
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
                if isinstance(principal, GuestPrincipal) and guest_finish_required:
                    finish_guest_action(
                        platform_user_id=principal.platform_user_id,
                        client_action_id=request_id,
                        success=False,
                    )
            yield _sse(
                "turn.failed",
                {
                    "version": 1,
                    "turn_id": turn_id,
                    "code": "stream_internal_error",
                    "retryable": True,
                    "charged_coins": 0,
                    "wallet": (
                        None if isinstance(principal, GuestPrincipal)
                        else _wallet(principal.platform_user_id)
                    ),
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
                if isinstance(principal, GuestPrincipal) and guest_finish_required:
                    finish_guest_action(
                        platform_user_id=principal.platform_user_id,
                        client_action_id=request_id,
                        success=saw_delta,
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
