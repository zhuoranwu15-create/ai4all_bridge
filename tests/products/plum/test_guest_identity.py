"""Plum Guest subject, session, profile and auth-context contracts."""
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.infrastructure import guest_repository, repository
from app.products.plum.manifest import install_public_routes


def _guest_client(monkeypatch, fresh_db) -> TestClient:
    config = MagicMock(wraps=fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = False
    config.plum_public_test_auth_enabled = True
    config.plum_session_cookie_name = "plum_session"
    config.plum_csrf_cookie_name = "plum_csrf"
    config.plum_session_cookie_secure = False
    config.plum_session_days = 30
    config.plum_guest_chat_enabled = True
    config.plum_email_auth_enabled = False
    config.plum_google_auth_enabled = False
    config.plum_apple_auth_enabled = False
    config.plum_guest_session_cookie_name = "plum_guest_session"
    config.plum_guest_session_days = 30
    config.plum_guest_typed_limit = 2
    config.plum_guest_continue_limit = 8
    config.plum_guest_character_continue_limit = 2
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    monkeypatch.setattr(guest_repository, "settings", config)
    app = FastAPI()
    install_public_routes(app)
    return TestClient(app)


def test_m0077_backfills_members_and_adds_guest_tables(test_settings, empty_pg_database):
    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=76, expected_current_version=0)
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_pre_guest_migration', 'legacy@local.invalid', 'Legacy')
                """
            )

        db.migrate_db_through(target_version=77, expected_current_version=76)
        with db.connect() as conn:
            user = conn.execute(
                "SELECT subject_kind, merged_into_platform_user_id FROM platform_users WHERE id='pusr_pre_guest_migration'"
            ).fetchone()
            assert user["subject_kind"] == "member"
            assert user["merged_into_platform_user_id"] is None
            for table in (
                "plum_guest_sessions",
                "plum_guest_profiles",
                "plum_guest_usage",
                "plum_guest_character_usage",
                "plum_guest_action_receipts",
            ):
                conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchall()
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, subject_kind)
                VALUES ('pusr_nullable_guest', NULL, 'guest')
                """
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    """
                    INSERT INTO platform_users(id, phone, subject_kind)
                    VALUES ('pusr_invalid_merged', NULL, 'merged')
                    """
                )
    db.close_pg_pool()


def test_visitor_context_and_disabled_capabilities(fresh_db, monkeypatch):
    client = _guest_client(monkeypatch, fresh_db)
    with client:
        context = client.get("/api/v1/products/plum/auth/context")

    assert context.status_code == 200
    assert context.json()["actor"] == {
        "kind": "visitor",
        "user": None,
        "profile_complete": False,
    }
    assert context.json()["capabilities"] == {
        "guest_chat": True,
        "email_auth": False,
        "google_auth": False,
        "apple_auth": False,
        "invite_auth": True,
    }


def test_guest_session_is_hashed_and_has_no_membership_or_reward(
    fresh_db, monkeypatch
):
    client = _guest_client(monkeypatch, fresh_db)
    with client:
        created = client.post(
            "/api/v1/products/plum/auth/guest/session",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )

        assert created.status_code == 200
        body = created.json()
        assert body["actor"]["kind"] == "guest"
        assert body["guest_quota"] == {
            "typed_remaining": 2,
            "continue_remaining": 8,
        }
        guest_id = body["actor"]["user"]["id"]
        raw_token = client.cookies.get("plum_guest_session")
        assert raw_token
        assert client.cookies.get("plum_csrf")

        restored = client.post(
            "/api/v1/products/plum/auth/guest/session",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert restored.status_code == 200
        assert restored.json()["actor"]["user"]["id"] == guest_id

    with db.connect() as conn:
        user = conn.execute(
            "SELECT phone, subject_kind FROM platform_users WHERE id=?", (guest_id,)
        ).fetchone()
        session = conn.execute(
            "SELECT token_hash FROM plum_guest_sessions WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()
        assert user["phone"] is None
        assert user["subject_kind"] == "guest"
        assert session["token_hash"] != raw_token
        assert len(session["token_hash"]) == 64
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM product_memberships WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM entitlement_ledger WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()["n"] == 0


def test_guest_profile_requires_adult_confirmation_and_csrf(fresh_db, monkeypatch):
    client = _guest_client(monkeypatch, fresh_db)
    payload = {
        "adult_confirmed": True,
        "pronouns": "they_them",
        "relationship_preference": "no_preference",
        "genres": ["fantasy", "romance"],
    }
    with client:
        client.post(
            "/api/v1/products/plum/auth/guest/session",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        missing_csrf = client.patch(
            "/api/v1/products/plum/auth/guest/profile", json=payload
        )
        assert missing_csrf.status_code == 403

        csrf = client.cookies.get("plum_csrf")
        underage = client.patch(
            "/api/v1/products/plum/auth/guest/profile",
            headers={"X-Plum-CSRF": csrf},
            json={**payload, "adult_confirmed": False},
        )
        assert underage.status_code == 403
        assert underage.json()["detail"] == "adult_confirmation_required"

        saved = client.patch(
            "/api/v1/products/plum/auth/guest/profile",
            headers={"X-Plum-CSRF": csrf},
            json=payload,
        )
        assert saved.status_code == 200
        assert saved.json()["actor"]["profile_complete"] is True
        assert saved.json()["actor"]["profile"]["genres"] == ["fantasy", "romance"]


def test_guest_creation_rejects_cross_site_and_member_takes_precedence(
    fresh_db, monkeypatch
):
    client = _guest_client(monkeypatch, fresh_db)
    with client:
        rejected = client.post(
            "/api/v1/products/plum/auth/guest/session",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert rejected.status_code == 403

        invite = repository.create_plum_access_invite(label="member precedence")
        login = client.post(
            "/api/v1/products/plum/auth/access-code",
            json={"access_code": invite["access_code"], "display_name": "Member"},
        )
        assert login.status_code == 200
        member_id = login.json()["user"]["id"]
        context = client.get("/api/v1/products/plum/auth/context")
        assert context.json()["actor"]["kind"] == "member"
        assert context.json()["actor"]["user"]["id"] == member_id

        no_downgrade = client.post(
            "/api/v1/products/plum/auth/guest/session",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert no_downgrade.status_code == 200
        assert no_downgrade.json()["actor"]["kind"] == "member"
