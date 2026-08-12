"""Email OTP registration and in-place Guest promotion contracts."""
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.db as db
from app.db._backend import IntegrityError
from app.db._core import (
    _MIGRATIONS,
    _migration_0078_plum_external_identity_challenges,
    _migration_0079_plum_identity_merge_constraints,
)
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.application import identity as identity_application
from app.products.plum.infrastructure import (
    guest_repository,
    identity_repository,
    repository,
)
from app.products.plum.manifest import install_public_routes


def _client(monkeypatch, fresh_db):
    config = MagicMock(wraps=fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = False
    config.plum_public_test_auth_enabled = True
    config.plum_session_cookie_name = "plum_session"
    config.plum_guest_session_cookie_name = "plum_guest_session"
    config.plum_csrf_cookie_name = "plum_csrf"
    config.plum_session_cookie_secure = False
    config.plum_session_days = 30
    config.plum_guest_session_days = 30
    config.plum_guest_chat_enabled = True
    config.plum_email_auth_enabled = True
    config.plum_google_auth_enabled = False
    config.plum_apple_auth_enabled = False
    config.plum_guest_typed_limit = 2
    config.plum_guest_continue_limit = 8
    config.plum_guest_character_continue_limit = 2
    config.plum_guest_provider_id = "deepseek"
    config.plum_guest_max_output_tokens = 384
    config.plum_fast_provider_id = "deepseek"
    config.plum_balanced_provider_id = "chatgpt"
    config.plum_immersive_provider_id = "deepseek-v4-pro"
    config.plum_email_otp_pepper = "test-only-email-pepper-32-bytes-minimum"
    config.plum_email_otp_expires_minutes = 10
    config.plum_email_otp_max_attempts = 3
    config.plum_email_otp_resend_seconds = 60
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    monkeypatch.setattr(guest_repository, "settings", config)
    monkeypatch.setattr(identity_repository, "settings", config)
    monkeypatch.setattr(repository, "settings", config)
    repository.seed_plum_catalog()
    app = FastAPI()
    install_public_routes(app)
    return TestClient(app)


def _onboard(client):
    client.post(
        "/api/v1/products/plum/auth/guest/session",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    csrf = client.cookies.get("plum_csrf")
    profile = client.patch(
        "/api/v1/products/plum/auth/guest/profile",
        headers={"X-Plum-CSRF": csrf},
        json={"adult_confirmed": True, "pronouns": "they_them", "genres": []},
    )
    assert profile.status_code == 200
    return csrf, profile.json()["actor"]["user"]["id"]


def test_m0078_adds_external_identity_and_challenge_schema(
    test_settings, empty_pg_database
):
    assert (78, _migration_0078_plum_external_identity_challenges) in _MIGRATIONS
    assert _MIGRATIONS[-1] == (
        79,
        _migration_0079_plum_identity_merge_constraints,
    )
    with patch("app.db.settings", test_settings):
        db.migrate_db_through(target_version=77, expected_current_version=0)
        db.migrate_db_through(target_version=78, expected_current_version=77)
        db.migrate_db_through(target_version=79, expected_current_version=78)
        with db.connect() as conn:
            for table in (
                "platform_external_identities",
                "plum_identity_challenges",
                "plum_identity_merge_runs",
            ):
                conn.execute(f"SELECT 1 FROM {table} WHERE 1=0").fetchall()
            conn.execute(
                """
                INSERT INTO platform_users(id, phone, display_name)
                VALUES ('pusr_email_schema', NULL, 'Email Schema')
                """
            )
            deferrable = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM pg_constraint
                WHERE conrelid IN (
                    'plum_connections'::regclass,
                    'plum_connection_runtime_bindings'::regclass,
                    'plum_storylines'::regclass,
                    'plum_storyline_state'::regclass,
                    'plum_conversations'::regclass
                )
                  AND contype='f' AND condeferrable
                  AND pg_get_constraintdef(oid) LIKE '%platform_user_id%'
                """
            ).fetchone()
            assert deferrable["n"] >= 7
            conn.execute(
                """
                INSERT INTO platform_external_identities(
                    id, platform_user_id, provider, provider_subject
                ) VALUES ('peid_schema_1', 'pusr_email_schema', 'email',
                          'unique@example.com')
                """
            )
        with pytest.raises(IntegrityError):
            with db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO platform_external_identities(
                        id, platform_user_id, provider, provider_subject
                    ) VALUES ('peid_schema_2', 'pusr_email_schema', 'email',
                              'unique@example.com')
                    """
                )
    db.close_pg_pool()


def test_email_otp_promotes_guest_in_place_and_preserves_conversation(
    fresh_db, monkeypatch
):
    client = _client(monkeypatch, fresh_db)
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    with client:
        csrf, guest_id = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        challenge = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "  New.User@Example.COM "},
        )
        verified = client.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf},
            json={
                "challenge_id": challenge.json()["challenge_id"],
                "code": sent["code"],
                "preferred_name": "Alex",
            },
        )

    assert challenge.status_code == 202
    assert "code" not in challenge.json()
    assert sent["email"] == "new.user@example.com"
    assert verified.status_code == 200
    body = verified.json()
    assert body["actor"]["kind"] == "member"
    assert body["actor"]["user"] == {"id": guest_id, "display_name": "Alex"}
    assert body["merge"]["mode"] == "promoted"
    assert body["grant"] == {"amount": 1000, "was_applied": True}
    assert body["wallet"]["balance"] == 1000
    assert client.cookies.get("plum_session")
    assert client.cookies.get("plum_guest_session") is None
    with db.connect() as conn:
        user = conn.execute(
            "SELECT subject_kind, display_name FROM platform_users WHERE id=?",
            (guest_id,),
        ).fetchone()
        identity = conn.execute(
            "SELECT provider_subject, email_verified FROM platform_external_identities WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()
        persisted_conversation = conn.execute(
            "SELECT platform_user_id, runtime_account_id, model_profile FROM plum_conversations WHERE id=?",
            (conversation["id"],),
        ).fetchone()
        ownership = conn.execute(
            "SELECT owner_kind FROM runtime_ownerships WHERE runtime_account_id=?",
            (conversation["runtime_account_id"],),
        ).fetchone()
    assert dict(user) == {"subject_kind": "member", "display_name": "Alex"}
    assert dict(identity) == {
        "provider_subject": "new.user@example.com",
        "email_verified": 1,
    }
    assert persisted_conversation["platform_user_id"] == guest_id
    assert persisted_conversation["runtime_account_id"] == conversation["runtime_account_id"]
    assert persisted_conversation["model_profile"] != "guest_free"
    assert ownership["owner_kind"] == "resident"


def test_email_otp_attempt_limit_and_actor_binding(fresh_db, monkeypatch):
    client_a = _client(monkeypatch, fresh_db)
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    with client_a:
        csrf_a, _ = _onboard(client_a)
        challenge = client_a.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf_a},
            json={"email": "bound@example.com"},
        ).json()["challenge_id"]
        wrong = []
        for _ in range(3):
            wrong.append(
                client_a.post(
                    "/api/v1/products/plum/auth/email/verify",
                    headers={"X-Plum-CSRF": csrf_a},
                    json={"challenge_id": challenge, "code": "000000"},
                )
            )
        correct_after_limit = client_a.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf_a},
            json={"challenge_id": challenge, "code": sent["code"]},
        )
    assert [item.status_code for item in wrong] == [400, 400, 400]
    assert wrong[-1].json()["detail"] == "email_challenge_attempts_exceeded"
    assert correct_after_limit.status_code == 409
    assert correct_after_limit.json()["detail"] == "email_challenge_consumed"


def test_email_delivery_failure_invalidates_challenge(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)

    def fail(**kwargs):
        del kwargs
        raise plum_api.PlumEmailDeliveryUnavailable("down")

    monkeypatch.setattr(plum_api, "send_login_code", fail)
    with client:
        csrf, guest_id = _onboard(client)
        response = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "down@example.com"},
        )
    assert response.status_code == 503
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM plum_identity_challenges WHERE guest_platform_user_id=?",
            (guest_id,),
        ).fetchone()
    assert row["status"] == "failed"


def test_email_verify_recovers_after_session_issue_without_duplicate_grant(
    fresh_db, monkeypatch
):
    client = _client(monkeypatch, fresh_db)
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    original_create_session = identity_application.create_platform_user_session
    calls = {"count": 0}

    def fail_first_session(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary session store failure")
        return original_create_session(**kwargs)

    monkeypatch.setattr(
        identity_application,
        "create_platform_user_session",
        fail_first_session,
    )
    with client:
        csrf, guest_id = _onboard(client)
        challenge = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "retry@example.com"},
        ).json()["challenge_id"]
        payload = {
            "challenge_id": challenge,
            "code": sent["code"],
            "preferred_name": "Retry User",
        }
        with pytest.raises(RuntimeError, match="temporary session store failure"):
            client.post(
                "/api/v1/products/plum/auth/email/verify",
                headers={"X-Plum-CSRF": csrf},
                json=payload,
            )
        with db.connect() as conn:
            interrupted = conn.execute(
                """
                SELECT c.status AS challenge_status, s.status AS guest_session_status,
                       u.subject_kind
                FROM plum_identity_challenges c
                JOIN plum_guest_sessions s ON s.platform_user_id=c.guest_platform_user_id
                JOIN platform_users u ON u.id=c.guest_platform_user_id
                WHERE c.id=?
                """,
                (challenge,),
            ).fetchone()
        retried = client.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf},
            json=payload,
        )

    assert dict(interrupted) == {
        "challenge_status": "pending",
        "guest_session_status": "active",
        "subject_kind": "member",
    }
    assert retried.status_code == 200
    assert retried.json()["grant"] == {"amount": 1000, "was_applied": True}
    with db.connect() as conn:
        counts = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE source_type='new_user_grant') AS grants,
                COUNT(*) FILTER (WHERE provider='email') AS identities
            FROM entitlement_ledger
            FULL JOIN platform_external_identities ON FALSE
            WHERE entitlement_ledger.platform_user_id=?
               OR platform_external_identities.platform_user_id=?
            """,
            (guest_id, guest_id),
        ).fetchone()
    assert counts["grants"] == 1
    assert counts["identities"] == 1


def _create_existing_email_member(email: str) -> str:
    member_id = "pusr_returning_email"
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, status, subject_kind)
            VALUES (?, NULL, 'Returning Member', 'active', 'member')
            """,
            (member_id,),
        )
    repository.ensure_plum_user(
        platform_user_id=member_id,
        display_name="Returning Member",
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_external_identities(
                id, platform_user_id, provider, provider_subject,
                normalized_email, email_verified
            ) VALUES ('peid_returning_email', ?, 'email', ?, ?, 1)
            """,
            (member_id, email, email),
        )
    return member_id


def test_email_otp_merges_guest_into_returning_member_with_guest_story_active(
    fresh_db, monkeypatch
):
    client = _client(monkeypatch, fresh_db)
    member_id = _create_existing_email_member("returning@example.com")
    member_conversation = repository.create_or_get_conversation(
        platform_user_id=member_id,
        character_id="char_ref_after_hours",
    )
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    with client:
        csrf, guest_id = _onboard(client)
        guest_shared = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        guest_other = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_dangerous_promise"},
        ).json()["conversation"]
        challenge = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "returning@example.com"},
        ).json()["challenge_id"]
        verified = client.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf},
            json={"challenge_id": challenge, "code": sent["code"]},
        )

    assert verified.status_code == 200
    assert verified.json()["actor"]["user"] == {
        "id": member_id,
        "display_name": "Returning Member",
    }
    assert verified.json()["merge"] == {
        "mode": "merged",
        "conversations_moved": 2,
    }
    assert verified.json()["grant"] == {"amount": 1000, "was_applied": False}
    assert client.cookies.get("plum_guest_session") is None
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, platform_user_id, status, runtime_account_id
            FROM plum_conversations
            WHERE id IN (?, ?, ?)
            ORDER BY id
            """,
            (
                member_conversation["id"],
                guest_shared["id"],
                guest_other["id"],
            ),
        ).fetchall()
        guest_user = conn.execute(
            "SELECT subject_kind, merged_into_platform_user_id FROM platform_users WHERE id=?",
            (guest_id,),
        ).fetchone()
        ownership = conn.execute(
            """
            SELECT platform_user_id, owner_kind, source_type
            FROM runtime_ownerships WHERE runtime_account_id=?
            """,
            (guest_shared["runtime_account_id"],),
        ).fetchone()
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM entitlement_ledger
                 WHERE platform_user_id=? AND source_type='new_user_grant') AS grants,
                (SELECT COUNT(*) FROM plum_identity_merge_runs
                 WHERE guest_platform_user_id=? AND status='completed') AS merge_runs
            """,
            (member_id, guest_id),
        ).fetchone()
    by_id = {str(row["id"]): row for row in rows}
    assert by_id[member_conversation["id"]]["status"] == "archived"
    assert by_id[guest_shared["id"]]["status"] == "active"
    assert by_id[guest_other["id"]]["status"] == "active"
    assert by_id[guest_shared["id"]]["platform_user_id"] == member_id
    assert dict(guest_user) == {
        "subject_kind": "merged",
        "merged_into_platform_user_id": member_id,
    }
    assert dict(ownership) == {
        "platform_user_id": member_id,
        "owner_kind": "resident",
        "source_type": "guest_import",
    }
    assert dict(counts) == {"grants": 1, "merge_runs": 1}


def test_returning_email_merge_recovers_after_session_issue(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    member_id = _create_existing_email_member("recover-merge@example.com")
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    original_create_session = identity_application.create_platform_user_session
    calls = {"count": 0}

    def fail_first_session(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary merge session failure")
        return original_create_session(**kwargs)

    monkeypatch.setattr(
        identity_application,
        "create_platform_user_session",
        fail_first_session,
    )
    with client:
        csrf, guest_id = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_dangerous_promise"},
        ).json()["conversation"]
        challenge = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "recover-merge@example.com"},
        ).json()["challenge_id"]
        payload = {"challenge_id": challenge, "code": sent["code"]}
        with pytest.raises(RuntimeError, match="temporary merge session failure"):
            client.post(
                "/api/v1/products/plum/auth/email/verify",
                headers={"X-Plum-CSRF": csrf},
                json=payload,
            )
        retried = client.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf},
            json=payload,
        )

    assert retried.status_code == 200
    assert retried.json()["actor"]["user"]["id"] == member_id
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT c.platform_user_id,
                   (SELECT COUNT(*) FROM plum_identity_merge_runs
                    WHERE guest_platform_user_id=?) AS merge_runs,
                   (SELECT COUNT(*) FROM entitlement_ledger
                    WHERE platform_user_id=? AND source_type='new_user_grant') AS grants
            FROM plum_conversations c WHERE c.id=?
            """,
            (guest_id, member_id, conversation["id"]),
        ).fetchone()
    assert dict(row) == {
        "platform_user_id": member_id,
        "merge_runs": 1,
        "grants": 1,
    }


def test_returning_email_disabled_membership_keeps_guest_unchanged(
    fresh_db, monkeypatch
):
    client = _client(monkeypatch, fresh_db)
    member_id = _create_existing_email_member("disabled@example.com")
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE product_memberships SET status='disabled'
            WHERE platform_user_id=? AND app_id='plum'
            """,
            (member_id,),
        )
    sent = {}
    monkeypatch.setattr(
        plum_api,
        "send_login_code",
        lambda **kwargs: sent.update(kwargs),
    )
    with client:
        csrf, guest_id = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_dangerous_promise"},
        ).json()["conversation"]
        challenge = client.post(
            "/api/v1/products/plum/auth/email/challenges",
            headers={"X-Plum-CSRF": csrf},
            json={"email": "disabled@example.com"},
        ).json()["challenge_id"]
        response = client.post(
            "/api/v1/products/plum/auth/email/verify",
            headers={"X-Plum-CSRF": csrf},
            json={"challenge_id": challenge, "code": sent["code"]},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "identity_target_disabled"
    with db.connect() as conn:
        unchanged = conn.execute(
            """
            SELECT u.subject_kind, s.status AS guest_session_status,
                   c.platform_user_id, challenge.status AS challenge_status,
                   (SELECT COUNT(*) FROM plum_identity_merge_runs
                    WHERE guest_platform_user_id=?) AS merge_runs
            FROM platform_users u
            JOIN plum_guest_sessions s ON s.platform_user_id=u.id
            JOIN plum_conversations c ON c.platform_user_id=u.id
            JOIN plum_identity_challenges challenge
              ON challenge.guest_platform_user_id=u.id
            WHERE u.id=? AND c.id=? AND challenge.id=?
            """,
            (guest_id, guest_id, conversation["id"], challenge),
        ).fetchone()
    assert dict(unchanged) == {
        "subject_kind": "guest",
        "guest_session_status": "active",
        "platform_user_id": guest_id,
        "challenge_status": "pending",
        "merge_runs": 0,
    }
