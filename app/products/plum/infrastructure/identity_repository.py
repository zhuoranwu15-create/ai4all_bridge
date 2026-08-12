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


def resolve_email_identity(*, normalized_email: str) -> Optional[Dict[str, Any]]:
    email = normalize_email(normalized_email)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT identity.*, users.display_name, users.status AS user_status,
                   users.subject_kind, membership.status AS membership_status
            FROM platform_external_identities identity
            JOIN platform_users users ON users.id=identity.platform_user_id
            LEFT JOIN product_memberships membership
              ON membership.platform_user_id=users.id AND membership.app_id=?
            WHERE identity.provider='email' AND identity.provider_subject=?
              AND identity.status='active'
            """,
            (PLUM_APP_ID, email),
        ).fetchone()
    return dict(row) if row is not None else None


def merge_guest_into_email_member(
    *,
    challenge_id: str,
    guest_platform_user_id: str,
    target_platform_user_id: str,
    normalized_email: str,
) -> Dict[str, Any]:
    """Move the explicit Guest-owned Plum aggregate to one existing Member."""

    email = normalize_email(normalized_email)
    if guest_platform_user_id == target_platform_user_id:
        raise ValueError("identity_merge_same_subject")
    merge_run_id = f"pimr_{uuid.uuid4().hex}"
    with connect() as conn:
        conn.execute("SET CONSTRAINTS ALL DEFERRED")
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-external-identity:email:{email}",),
        )
        for platform_user_id in sorted(
            (guest_platform_user_id, target_platform_user_id)
        ):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                (f"plum-user-merge:{platform_user_id}",),
            )
        challenge = conn.execute(
            "SELECT * FROM plum_identity_challenges WHERE id=? FOR UPDATE",
            (challenge_id,),
        ).fetchone()
        if challenge is None or str(challenge["status"]) != "pending":
            raise ValueError("email_challenge_consumed")
        if str(challenge["guest_platform_user_id"] or "") != guest_platform_user_id:
            raise ValueError("email_challenge_actor_mismatch")
        identity = conn.execute(
            """
            SELECT * FROM platform_external_identities
            WHERE provider='email' AND provider_subject=? AND status='active'
            FOR UPDATE
            """,
            (email,),
        ).fetchone()
        if identity is None or str(identity["platform_user_id"]) != target_platform_user_id:
            raise ValueError("email_identity_changed")
        users = conn.execute(
            "SELECT id, status, subject_kind FROM platform_users WHERE id IN (?, ?) FOR UPDATE",
            (guest_platform_user_id, target_platform_user_id),
        ).fetchall()
        by_id = {str(row["id"]): row for row in users}
        guest = by_id.get(guest_platform_user_id)
        target = by_id.get(target_platform_user_id)
        existing_run = conn.execute(
            "SELECT * FROM plum_identity_merge_runs WHERE guest_platform_user_id=? FOR UPDATE",
            (guest_platform_user_id,),
        ).fetchone()
        is_recovery = (
            existing_run is not None
            and str(existing_run["status"]) == "pending"
            and str(existing_run["target_platform_user_id"])
            == target_platform_user_id
            and str(existing_run["identity_id"]) == str(identity["id"])
        )
        if (
            guest is None
            or str(guest["status"]) != "active"
            or str(guest["subject_kind"])
            not in ({"merged"} if is_recovery else {"guest"})
        ):
            raise ValueError("guest_not_mergeable")
        if (
            target is None
            or str(target["status"]) != "active"
            or str(target["subject_kind"]) != "member"
        ):
            raise ValueError("identity_target_disabled")
        membership = conn.execute(
            """
            SELECT status FROM product_memberships
            WHERE platform_user_id=? AND app_id=? FOR UPDATE
            """,
            (target_platform_user_id, PLUM_APP_ID),
        ).fetchone()
        if membership is None or str(membership["status"]) != "active":
            raise ValueError("identity_target_disabled")
        if existing_run is not None:
            if (
                str(existing_run["target_platform_user_id"])
                != target_platform_user_id
                or str(existing_run["identity_id"]) != str(identity["id"])
            ):
                raise ValueError("identity_merge_conflict")
            merge_run_id = str(existing_run["id"])
            if is_recovery:
                metadata = challenge["metadata_json"]
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                prior = dict(dict(metadata or {}).get("merge") or {})
                return {
                    "platform_user_id": target_platform_user_id,
                    "merge_run_id": merge_run_id,
                    "merge": {
                        "mode": "merged",
                        "conversations_moved": int(
                            prior.get("conversations_moved", 0)
                        ),
                    },
                }
            raise ValueError("email_challenge_consumed")
        else:
            conn.execute(
                """
                INSERT INTO plum_identity_merge_runs(
                    id, guest_platform_user_id, target_platform_user_id,
                    identity_id, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    merge_run_id,
                    guest_platform_user_id,
                    target_platform_user_id,
                    identity["id"],
                ),
            )

        guest_connections = conn.execute(
            """
            SELECT id, character_id FROM plum_connections
            WHERE platform_user_id=? AND status='active'
            ORDER BY id
            """,
            (guest_platform_user_id,),
        ).fetchall()
        moved = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM plum_conversations WHERE platform_user_id=?",
                (guest_platform_user_id,),
            ).fetchone()["n"]
        )
        for connection in guest_connections:
            target_connections = conn.execute(
                """
                SELECT id FROM plum_connections
                WHERE platform_user_id=? AND character_id=? AND status='active'
                ORDER BY updated_at DESC, id
                FOR UPDATE
                """,
                (target_platform_user_id, connection["character_id"]),
            ).fetchall()
            for target_connection in target_connections:
                conn.execute(
                    """
                    UPDATE plum_conversations
                    SET status='archived', archived_at=COALESCE(
                            archived_at,
                            to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                        ),
                        updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE connection_id=? AND status='active'
                    """,
                    (target_connection["id"],),
                )
                conn.execute(
                    """
                    UPDATE plum_storylines
                    SET status='archived', archived_at=COALESCE(
                            archived_at,
                            to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                        ),
                        updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE connection_id=? AND status='active'
                    """,
                    (target_connection["id"],),
                )
                conn.execute(
                    """
                    UPDATE plum_connections
                    SET status='archived', archived_at=COALESCE(
                            archived_at,
                            to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                        ),
                        updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                    WHERE id=? AND status='active'
                    """,
                    (target_connection["id"],),
                )

        # A Member can have only one active default Persona. Keep their default;
        # import the Guest Persona without rewriting immutable prompt/profile data.
        conn.execute(
            """
            UPDATE plum_user_personas SET is_default=0
            WHERE platform_user_id=? AND status='active' AND is_default=1
            """,
            (guest_platform_user_id,),
        )
        conn.execute(
            "UPDATE plum_user_personas SET platform_user_id=? WHERE platform_user_id=?",
            (target_platform_user_id, guest_platform_user_id),
        )
        owner_tables = (
            "plum_connections",
            "plum_connection_runtime_bindings",
            "plum_storylines",
            "plum_storyline_state",
            "plum_conversations",
        )
        for table in owner_tables:
            conn.execute(
                f"UPDATE {table} SET platform_user_id=? WHERE platform_user_id=?",
                (target_platform_user_id, guest_platform_user_id),
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
            (target_platform_user_id,),
        )
        conn.execute(
            """
            UPDATE runtime_ownerships
            SET platform_user_id=?, owner_kind='resident', source_type='guest_import',
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE platform_user_id=? AND app_id=? AND owner_kind='guest' AND status='active'
            """,
            (target_platform_user_id, guest_platform_user_id, PLUM_APP_ID),
        )
        conn.execute(
            "UPDATE sessions SET sender_id=? WHERE sender_id=?",
            (target_platform_user_id, guest_platform_user_id),
        )
        conn.execute(
            """
            UPDATE platform_users
            SET subject_kind='merged', merged_into_platform_user_id=?,
                merged_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=?
            """,
            (target_platform_user_id, guest_platform_user_id),
        )
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
                        "merge": {
                            "run_id": merge_run_id,
                            "target_platform_user_id": target_platform_user_id,
                            "conversations_moved": moved,
                        }
                    }
                ),
                challenge_id,
            ),
        )
    return {
        "platform_user_id": target_platform_user_id,
        "merge_run_id": merge_run_id,
        "merge": {"mode": "merged", "conversations_moved": moved},
    }


def finalize_email_merge(
    *, challenge_id: str, guest_platform_user_id: str, merge_run_id: str
) -> None:
    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"plum-identity-challenge:{challenge_id}",),
        )
        run = conn.execute(
            "SELECT * FROM plum_identity_merge_runs WHERE id=? FOR UPDATE",
            (merge_run_id,),
        ).fetchone()
        challenge = conn.execute(
            "SELECT * FROM plum_identity_challenges WHERE id=? FOR UPDATE",
            (challenge_id,),
        ).fetchone()
        if run is None or challenge is None or str(challenge["status"]) != "pending":
            raise ValueError("email_challenge_consumed")
        if str(run["guest_platform_user_id"]) != guest_platform_user_id:
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
        conn.execute(
            """
            UPDATE plum_identity_merge_runs
            SET status='completed', completed_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                result_json=?::jsonb,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE id=? AND status='pending'
            """,
            (
                json.dumps(
                    {
                        "target_platform_user_id": run["target_platform_user_id"],
                        "guest_platform_user_id": guest_platform_user_id,
                    }
                ),
                merge_run_id,
            ),
        )


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
    "finalize_email_merge",
    "finalize_email_promotion",
    "merge_guest_into_email_member",
    "normalize_email",
    "promote_guest_with_email",
    "resolve_email_identity",
    "verify_email_challenge",
]
