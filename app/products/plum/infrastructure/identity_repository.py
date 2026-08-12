"""Plum external identity challenge storage and Guest promotion primitives."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
import uuid
from typing import Any, Dict, Optional

from app.bootstrap.product_registry import PLUM_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.config import settings
from app.db import connect
from app.db.product_memberships import _ensure_product_membership_in_conn

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(email: str) -> str:
    cleaned = unicodedata.normalize("NFKC", str(email or "")).strip().lower()
    if len(cleaned) > 254 or not _EMAIL_RE.fullmatch(cleaned):
        raise ValueError("email_invalid")
    return cleaned


def _pepper() -> bytes:
    value = str(settings.plum_email_otp_pepper or "")
    if len(value) < 32:
        raise ValueError("email_auth_not_configured")
    return value.encode("utf-8")


def _digest(value: str) -> str:
    return hmac.new(_pepper(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_email_challenge(
    *, normalized_email: str, guest_platform_user_id: Optional[str]
) -> Dict[str, Any]:
    email = normalize_email(normalized_email)
    challenge_id = f"pich_{uuid.uuid4().hex}"
    code = f"{secrets.randbelow(1_000_000):06d}"
    target_hash = _digest(f"email:{email}")
    secret_hash = _digest(f"otp:{challenge_id}:{code}")
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-email-otp:{target_hash}",),
        )
        recent = conn.execute(
            """
            SELECT 1 FROM plum_identity_challenges
            WHERE provider='email' AND target_hash=? AND status='pending'
              AND created_at > to_char(
                    (now() AT TIME ZONE 'Asia/Shanghai') - (? || ' seconds')::interval,
                    'YYYY-MM-DD HH24:MI:SS'
                  )
            LIMIT 1
            """,
            (target_hash, f"+{max(1, int(settings.plum_email_otp_resend_seconds))}"),
        ).fetchone()
        if recent is not None:
            raise ValueError("email_challenge_too_frequent")
        conn.execute(
            """
            UPDATE plum_identity_challenges
            SET status='expired',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE provider='email' AND target_hash=? AND status='pending'
            """,
            (target_hash,),
        )
        conn.execute(
            """
            INSERT INTO plum_identity_challenges(
                id, kind, provider, target_hash, secret_hash,
                guest_platform_user_id, max_attempts, expires_at, metadata_json
            ) VALUES (?, 'email_otp', 'email', ?, ?, ?, ?,
                to_char((now() AT TIME ZONE 'Asia/Shanghai') + (? || ' minutes')::interval,
                        'YYYY-MM-DD HH24:MI:SS'),
                ?::jsonb)
            """,
            (
                challenge_id,
                target_hash,
                secret_hash,
                guest_platform_user_id,
                max(1, int(settings.plum_email_otp_max_attempts)),
                f"+{max(1, int(settings.plum_email_otp_expires_minutes))}",
                json.dumps({"normalized_email": email}),
            ),
        )
    return {
        "challenge_id": challenge_id,
        "code": code,
        "normalized_email": email,
        "expires_minutes": max(1, int(settings.plum_email_otp_expires_minutes)),
    }


def abandon_email_challenge(*, challenge_id: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE plum_identity_challenges
            SET status='failed',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status='pending'
            """,
            (challenge_id,),
        )


def verify_email_challenge(
    *, challenge_id: str, code: str, guest_platform_user_id: Optional[str]
) -> Dict[str, Any]:
    cleaned_code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", cleaned_code):
        raise ValueError("email_code_invalid")
    error: Optional[str] = None
    verified: Optional[Dict[str, Any]] = None
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-identity-challenge:{challenge_id}",),
        )
        row = conn.execute(
            "SELECT * FROM plum_identity_challenges WHERE id=? FOR UPDATE",
            (challenge_id,),
        ).fetchone()
        if row is None or str(row["provider"]) != "email":
            error = "email_challenge_invalid"
        elif str(row["status"]) != "pending":
            error = "email_challenge_consumed"
        elif str(row["expires_at"]) <= conn.execute(
            "SELECT to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS') AS now"
        ).fetchone()["now"]:
            conn.execute(
                "UPDATE plum_identity_challenges SET status='expired' WHERE id=?",
                (challenge_id,),
            )
            error = "email_challenge_expired"
        if error is None:
            bound_guest = str(row["guest_platform_user_id"] or "") or None
            if bound_guest != guest_platform_user_id:
                error = "email_challenge_actor_mismatch"
        if error is None and int(row["attempt_count"]) >= int(row["max_attempts"]):
            conn.execute(
                "UPDATE plum_identity_challenges SET status='failed' WHERE id=?",
                (challenge_id,),
            )
            error = "email_challenge_attempts_exceeded"
        if error is None and not hmac.compare_digest(
            str(row["secret_hash"]), _digest(f"otp:{challenge_id}:{cleaned_code}")
        ):
            attempts = int(row["attempt_count"]) + 1
            terminal = attempts >= int(row["max_attempts"])
            conn.execute(
                """
                UPDATE plum_identity_challenges
                SET attempt_count=?, status=?,
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=?
                """,
                (attempts, "failed" if terminal else "pending", challenge_id),
            )
            error = (
                "email_challenge_attempts_exceeded" if terminal else "email_code_invalid"
            )
        if error is None:
            metadata = row["metadata_json"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            verified = {
                "challenge_id": challenge_id,
                "normalized_email": normalize_email(
                    dict(metadata or {})["normalized_email"]
                ),
                "guest_platform_user_id": bound_guest,
            }
    if error is not None:
        raise ValueError(error)
    if verified is None:
        raise RuntimeError("email_challenge_verification_lost")
    return verified


def promote_guest_with_email(
    *,
    challenge_id: str,
    guest_platform_user_id: str,
    normalized_email: str,
    preferred_name: Optional[str],
) -> Dict[str, Any]:
    """Promote a Guest, or resume the same pending promotion idempotently."""

    email = normalize_email(normalized_email)
    name = str(preferred_name or "").strip()[:40] or email.split("@", 1)[0][:40] or "Plum User"
    identity_id = f"peid_{uuid.uuid4().hex}"
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-external-identity:email:{email}",),
        )
        challenge = conn.execute(
            "SELECT * FROM plum_identity_challenges WHERE id=? FOR UPDATE",
            (challenge_id,),
        ).fetchone()
        if challenge is None or str(challenge["status"]) != "pending":
            raise ValueError("email_challenge_consumed")
        if str(challenge["guest_platform_user_id"] or "") != guest_platform_user_id:
            raise ValueError("email_challenge_actor_mismatch")
        existing = conn.execute(
            """
            SELECT id, platform_user_id FROM platform_external_identities
            WHERE provider='email' AND provider_subject=? AND status='active'
            """,
            (email,),
        ).fetchone()
        is_recovery = (
            existing is not None
            and str(existing["platform_user_id"]) == guest_platform_user_id
        )
        if existing is not None and not is_recovery:
            raise ValueError("email_identity_merge_required")
        guest = conn.execute(
            "SELECT * FROM platform_users WHERE id=? FOR UPDATE",
            (guest_platform_user_id,),
        ).fetchone()
        if (
            guest is None
            or str(guest["status"]) != "active"
            or str(guest["subject_kind"]) not in ({"member"} if is_recovery else {"guest"})
        ):
            raise ValueError("guest_not_promotable")
        if is_recovery:
            identity_id = str(existing["id"])
            metadata = challenge["metadata_json"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            promotion = dict(dict(metadata or {}).get("promotion") or {})
            name = str(promotion.get("display_name") or guest["display_name"] or name)
        else:
            conn.execute(
                """
                UPDATE platform_users
                SET subject_kind='member', display_name=?,
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=?
                """,
                (name, guest_platform_user_id),
            )
            conn.execute(
                """
                INSERT INTO platform_external_identities(
                    id, platform_user_id, provider, provider_subject,
                    normalized_email, email_verified, last_authenticated_at
                ) VALUES (?, ?, 'email', ?, ?, 1,
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'))
                """,
                (identity_id, guest_platform_user_id, email, email),
            )
        membership = _ensure_product_membership_in_conn(
            conn,
            platform_user_id=guest_platform_user_id,
            app_id=PLUM_APP_ID,
            registry=PRODUCTION_PRODUCT_REGISTRY,
        )
        if membership["membership"]["status"] != "active":
            raise ValueError("product_membership_disabled")
        conn.execute(
            """
            UPDATE runtime_ownerships
            SET owner_kind=CASE WHEN source_type='plum_connection' THEN 'resident' ELSE 'product_entry' END,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id=? AND app_id=? AND owner_kind='guest' AND status='active'
            """,
            (guest_platform_user_id, PLUM_APP_ID),
        )
        conn.execute(
            """
            UPDATE plum_conversations SET model_profile=(
                SELECT profile FROM plum_model_profiles
                WHERE enabled=1 AND profile<>'guest_free'
                ORDER BY is_default DESC, coin_cost_micros LIMIT 1
            ), updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id=? AND status='active' AND model_profile='guest_free'
            """,
            (guest_platform_user_id,),
        )
        is_new_membership = bool(membership["is_new_membership"])
        if is_recovery:
            is_new_membership = bool(promotion.get("is_new_membership", False))
        else:
            conn.execute(
                """
                UPDATE plum_identity_challenges
                SET metadata_json=metadata_json || ?::jsonb,
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=? AND status='pending'
                """,
                (
                    json.dumps(
                        {
                            "promotion": {
                                "identity_id": identity_id,
                                "display_name": name,
                                "is_new_membership": is_new_membership,
                            }
                        }
                    ),
                    challenge_id,
                ),
            )
    return {
        "platform_user_id": guest_platform_user_id,
        "display_name": name,
        "identity_id": identity_id,
        "is_new_membership": is_new_membership,
        "merge": {"mode": "promoted", "conversations_moved": 0},
    }


def finalize_email_promotion(
    *, challenge_id: str, guest_platform_user_id: str
) -> None:
    """Consume the challenge only after provisioning and session issue succeed."""

    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-identity-challenge:{challenge_id}",),
        )
        challenge = conn.execute(
            "SELECT status, guest_platform_user_id FROM plum_identity_challenges WHERE id=? FOR UPDATE",
            (challenge_id,),
        ).fetchone()
        if challenge is None or str(challenge["status"]) != "pending":
            raise ValueError("email_challenge_consumed")
        if str(challenge["guest_platform_user_id"] or "") != guest_platform_user_id:
            raise ValueError("email_challenge_actor_mismatch")
        conn.execute(
            """
            UPDATE plum_guest_sessions
            SET status='promoted', promoted_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id=? AND status='active'
            """,
            (guest_platform_user_id,),
        )
        conn.execute(
            """
            UPDATE plum_identity_challenges
            SET status='consumed', consumed_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status='pending'
            """,
            (challenge_id,),
        )


__all__ = [
    "abandon_email_challenge",
    "create_email_challenge",
    "finalize_email_promotion",
    "normalize_email",
    "promote_guest_with_email",
    "verify_email_challenge",
]
