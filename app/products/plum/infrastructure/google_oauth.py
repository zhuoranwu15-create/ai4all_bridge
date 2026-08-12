"""Google OAuth Authorization Code + PKCE adapter.

The adapter deliberately performs full ID-token validation locally.  Google
tokens are never logged or returned to the caller.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15

from app.config import settings


class GoogleOAuthError(ValueError):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    display_name: str
    email_verified: bool
    profile: Dict[str, Any]


_jwks_cache: Dict[str, Any] = {"expires_at": 0.0, "keys": {}}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_pkce_pair() -> tuple[str, str]:
    verifier = _b64(__import__("secrets").token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def authorization_url(*, state: str, nonce: str, code_challenge: str) -> str:
    if not settings.plum_google_client_id or not settings.plum_google_redirect_uri:
        raise GoogleOAuthError("google_auth_not_configured")
    params = {
        "client_id": settings.plum_google_client_id,
        "redirect_uri": settings.plum_google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{settings.plum_google_authorization_endpoint}?{urlencode(params)}"


def _json_response(response: httpx.Response, name: str) -> Dict[str, Any]:
    if response.status_code >= 400:
        raise GoogleOAuthError(name)
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleOAuthError(name) from exc
    if not isinstance(payload, dict):
        raise GoogleOAuthError(name)
    return payload


def exchange_code(*, code: str, code_verifier: str) -> Dict[str, Any]:
    if not settings.plum_google_client_id or not settings.plum_google_client_secret:
        raise GoogleOAuthError("google_auth_not_configured")
    try:
        response = httpx.post(
            settings.plum_google_token_endpoint,
            data={
                "code": code,
                "client_id": settings.plum_google_client_id,
                "client_secret": settings.plum_google_client_secret,
                "redirect_uri": settings.plum_google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise GoogleOAuthError("google_token_exchange_failed") from exc
    return _json_response(response, "google_token_exchange_failed")


def _decode_part(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_jwks() -> Dict[str, Dict[str, Any]]:
    now = time.time()
    if now < float(_jwks_cache.get("expires_at", 0)):
        return dict(_jwks_cache["keys"])
    try:
        response = httpx.get(settings.plum_google_jwks_uri, timeout=10.0)
        payload = _json_response(response, "google_jwks_unavailable")
    except httpx.HTTPError as exc:
        raise GoogleOAuthError("google_jwks_unavailable") from exc
    keys = {str(item.get("kid")): item for item in payload.get("keys", []) if item.get("kid") and item.get("kty") == "RSA"}
    if not keys:
        raise GoogleOAuthError("google_jwks_unavailable")
    _jwks_cache.update({"expires_at": now + 3600, "keys": keys})
    return keys


def _verify_id_token(token: str, *, nonce: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise GoogleOAuthError("google_id_token_invalid")
    try:
        header = json.loads(_decode_part(parts[0]))
        claims = json.loads(_decode_part(parts[1]))
        signature = _decode_part(parts[2])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GoogleOAuthError("google_id_token_invalid") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise GoogleOAuthError("google_id_token_invalid")
    key = _load_jwks().get(str(header["kid"]))
    if key is None:
        _jwks_cache["expires_at"] = 0
        key = _load_jwks().get(str(header["kid"]))
    if key is None:
        raise GoogleOAuthError("google_id_token_invalid")
    try:
        public_key = RSAPublicNumbers(
            int.from_bytes(_decode_part(str(key["e"])), "big"),
            int.from_bytes(_decode_part(str(key["n"])), "big"),
        ).public_key()
        public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), PKCS1v15(), SHA256())
    except Exception as exc:
        raise GoogleOAuthError("google_id_token_signature_invalid") from exc
    now = int(time.time())
    audience = claims.get("aud")
    audience_ok = audience == settings.plum_google_client_id or (
        isinstance(audience, list) and settings.plum_google_client_id in audience
    )
    if claims.get("iss") != settings.plum_google_issuer or not audience_ok:
        raise GoogleOAuthError("google_id_token_claims_invalid")
    if claims.get("azp") and claims.get("azp") != settings.plum_google_client_id:
        raise GoogleOAuthError("google_id_token_claims_invalid")
    if claims.get("nonce") != nonce or int(claims.get("exp", 0)) <= now or int(claims.get("iat", 0)) > now + 60:
        raise GoogleOAuthError("google_id_token_claims_invalid")
    if not claims.get("sub") or not claims.get("email") or claims.get("email_verified") is not True:
        raise GoogleOAuthError("google_email_not_verified")
    return claims


def verify_token_response(payload: Dict[str, Any], *, nonce: str) -> GoogleIdentity:
    claims = _verify_id_token(str(payload.get("id_token") or ""), nonce=nonce)
    email = str(claims["email"]).strip().lower()
    name = str(claims.get("name") or claims.get("given_name") or email.split("@", 1)[0])[:40]
    return GoogleIdentity(str(claims["sub"]), email, name, True, {
        "sub": str(claims["sub"]), "email": email, "name": name,
        "picture": claims.get("picture"), "locale": claims.get("locale"),
    })


__all__ = ["GoogleIdentity", "GoogleOAuthError", "authorization_url", "create_pkce_pair", "exchange_code", "verify_token_response"]
