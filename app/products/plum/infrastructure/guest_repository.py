"""Persistence for provisional Plum Guest subjects.

Guest subjects deliberately have no product membership and receive no wallet
grant. Their opaque browser tokens are stored only as SHA-256 digests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.bootstrap.product_registry import PLUM_APP_ID
from app.config import settings
from app.db import connect


@dataclass(frozen=True)
class GuestPrincipal:
    guest_session_id: str
    platform_user_id: str
    app_id: str
    expires_at: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata_digest(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    secret = str(settings.ai4all_bridge_secret or "plum-guest-metadata")
    return hmac.new(secret.encode("utf-8"), cleaned.encode("utf-8"), hashlib.sha256).hexdigest()


def create_guest_session(
    *, days: int, client_ip: Optional[str], user_agent: Optional[str]
) -> Dict[str, Any]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    platform_user_id = f"pusr_guest_{uuid.uuid4().hex}"
    session_id = f"pgsess_{uuid.uuid4().hex}"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, status, subject_kind)
            VALUES (?, NULL, NULL, 'active', 'guest')
            """,
            (platform_user_id,),
        )
        conn.execute(
            """
            INSERT INTO plum_guest_sessions(
                id, token_hash, platform_user_id, csrf_hash, status,
                expires_at, last_seen_at, created_ip_hash, user_agent_hash
            )
            VALUES (?, ?, ?, ?, 'active',
                    to_char((now() AT TIME ZONE 'Asia/Shanghai') + (? || ' days')::interval, 'YYYY-MM-DD HH24:MI:SS'),
                    to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'), ?, ?)
            """,
            (
                session_id,
                _digest(token),
                platform_user_id,
                _digest(csrf_token),
                f"+{max(1, int(days))}",
                _metadata_digest(client_ip),
                _metadata_digest(user_agent),
            ),
        )
        conn.execute(
            "INSERT INTO plum_guest_usage(platform_user_id) VALUES (?)",
            (platform_user_id,),
        )
        row = conn.execute(
            "SELECT expires_at FROM plum_guest_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return {
        "principal": GuestPrincipal(
            guest_session_id=session_id,
            platform_user_id=platform_user_id,
            app_id=PLUM_APP_ID,
            expires_at=str(row["expires_at"]),
        ),
        "token": token,
        "csrf_token": csrf_token,
    }


def resolve_guest_principal(*, token: str) -> Optional[GuestPrincipal]:
    cleaned = str(token or "").strip()
    if not cleaned:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.platform_user_id, s.expires_at
            FROM plum_guest_sessions s
            JOIN platform_users pu ON pu.id=s.platform_user_id
            WHERE s.token_hash=? AND s.status='active'
              AND s.expires_at > to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
              AND pu.status='active' AND pu.subject_kind='guest'
            """,
            (_digest(cleaned),),
        ).fetchone()
        if row is not None:
            conn.execute(
                """
                UPDATE plum_guest_sessions
                SET last_seen_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=?
                """,
                (row["session_id"],),
            )
    if row is None:
        return None
    return GuestPrincipal(
        guest_session_id=str(row["session_id"]),
        platform_user_id=str(row["platform_user_id"]),
        app_id=PLUM_APP_ID,
        expires_at=str(row["expires_at"]),
    )


def guest_csrf_matches(*, session_id: str, csrf_token: str) -> bool:
    cleaned = str(csrf_token or "").strip()
    if not cleaned:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT csrf_hash FROM plum_guest_sessions WHERE id=? AND status='active'",
            (session_id,),
        ).fetchone()
    return bool(row) and hmac.compare_digest(str(row["csrf_hash"]), _digest(cleaned))


def get_guest_profile(*, platform_user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM plum_guest_profiles WHERE platform_user_id=?",
            (platform_user_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    raw_genres = result.pop("genres_json") or []
    if isinstance(raw_genres, str):
        raw_genres = json.loads(raw_genres)
    result["genres"] = list(raw_genres)
    return result


def upsert_guest_profile(
    *,
    platform_user_id: str,
    pronouns: str,
    relationship_preference: Optional[str],
    genres: list[str],
) -> Dict[str, Any]:
    with connect() as conn:
        subject = conn.execute(
            "SELECT subject_kind, status FROM platform_users WHERE id=?",
            (platform_user_id,),
        ).fetchone()
        if subject is None or subject["status"] != "active" or subject["subject_kind"] != "guest":
            raise ValueError("guest_session_expired")
        conn.execute(
            """
            INSERT INTO plum_guest_profiles(
                platform_user_id, adult_confirmed_at, pronouns,
                relationship_preference, genres_json
            )
            VALUES (?, to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'), ?, ?, ?::jsonb)
            ON CONFLICT(platform_user_id) DO UPDATE SET
                pronouns=excluded.pronouns,
                relationship_preference=excluded.relationship_preference,
                genres_json=excluded.genres_json,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            """,
            (platform_user_id, pronouns, relationship_preference, json.dumps(genres)),
        )
    profile = get_guest_profile(platform_user_id=platform_user_id)
    if profile is None:
        raise RuntimeError("guest profile was not saved")
    return profile


def get_guest_quota(*, platform_user_id: str) -> Dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            "SELECT typed_accepted, continue_accepted FROM plum_guest_usage WHERE platform_user_id=?",
            (platform_user_id,),
        ).fetchone()
    typed = int(row["typed_accepted"]) if row else 0
    continued = int(row["continue_accepted"]) if row else 0
    return {
        "typed_remaining": max(0, int(settings.plum_guest_typed_limit) - typed),
        "continue_remaining": max(0, int(settings.plum_guest_continue_limit) - continued),
    }


__all__ = [
    "GuestPrincipal",
    "create_guest_session",
    "get_guest_profile",
    "get_guest_quota",
    "guest_csrf_matches",
    "resolve_guest_principal",
    "upsert_guest_profile",
]
