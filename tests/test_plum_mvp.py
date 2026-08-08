"""Plum MVP 的产品隔离、会话和固定金币用例。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.db._core import _migration_0067_plum_product_rename, _table_exists
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.infrastructure import repository
from app.products.plum.manifest import install_public_routes


def _configure_plum(monkeypatch, fresh_db):
    config = MagicMock(wraps=fresh_db)
    config.plum_test_user_id = "user_plum_test"
    config.plum_test_phone = "plum-test@local.invalid"
    config.plum_fast_provider_id = "deepseek"
    config.plum_balanced_provider_id = "chatgpt"
    config.plum_immersive_provider_id = "deepseek-v4-pro"
    config.plum_public_test_auth_enabled = True
    config.plum_session_cookie_name = "plum_session"
    config.plum_csrf_cookie_name = "plum_csrf"
    config.plum_session_days = 30
    config.plum_session_cookie_secure = False
    monkeypatch.setattr(repository, "settings", config)
    return config


def test_plum_seed_is_idempotent_and_product_scoped(fresh_db, monkeypatch):
    _configure_plum(monkeypatch, fresh_db)

    first = repository.seed_plum_dev()
    second = repository.seed_plum_dev()

    assert first["platform_user_id"] == "user_plum_test"
    assert second["grant_balance_shells"] == "1000"
    characters = repository.list_characters()
    assert len(characters) == 10
    assert characters[0]["id"] == "char_ref_after_hours"
    assert characters[0]["creator"]["display_name"] == "plum"
    assert characters[0]["interaction_count"] == 48200
    assert characters[0]["capabilities"] == {"text": True, "voice": True}
    assert characters[0]["badges"][0]["code"] == "featured"
    assert [item["profile"] for item in repository.list_model_profiles()] == [
        "fast",
        "balanced",
        "immersive",
    ]
    membership = db.get_product_membership(
        platform_user_id="user_plum_test", app_id="plum"
    )
    assert membership["status"] == "active"
    wallet = db.get_wallet_summary(account_id=first["entry_account_id"])
    assert wallet["wallet"]["balance_shell_micros"] == 1_000_000_000


def test_plum_conversation_uses_shared_runtime_account_and_session(
    fresh_db, monkeypatch
):
    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()

    first = repository.create_or_get_conversation(
        platform_user_id="user_plum_test", character_id="char_ref_after_hours"
    )
    replay = repository.create_or_get_conversation(
        platform_user_id="user_plum_test", character_id="char_ref_after_hours"
    )

    assert replay["id"] == first["id"]
    assert first["model_profile"] == "balanced"
    assert first["character"]["display_name"] == "Kai · After Hours"
    assert repository.list_conversation_messages(first) == []
    with db.connect() as conn:
        assert (
            db.resolve_owner_platform_user_id(conn, first["runtime_account_id"])
            == "user_plum_test"
        )
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id=?",
            (first["runtime_account_id"],),
        ).fetchone()
        assert account["app_id"] == "plum"


def test_fixed_wallet_reservation_prevents_negative_balance_and_releases(
    fresh_db, monkeypatch
):
    _configure_plum(monkeypatch, fresh_db)
    seeded = repository.seed_plum_dev()
    account_id = seeded["entry_account_id"]

    first = db.reserve_fixed_shells(
        account_id=account_id,
        platform_user_id="user_plum_test",
        amount_shell_micros=5_000_000,
        idempotency_key="plum-test-reservation",
    )
    replay = db.reserve_fixed_shells(
        account_id=account_id,
        platform_user_id="user_plum_test",
        amount_shell_micros=5_000_000,
        idempotency_key="plum-test-reservation",
    )
    assert replay["id"] == first["id"]
    assert db.get_wallet_balance_shell_micros(account_id=account_id) == 995_000_000

    released = db.release_fixed_shell_reservation(
        account_id=account_id,
        platform_user_id="user_plum_test",
        reservation_idempotency_key="plum-test-reservation",
    )
    released_again = db.release_fixed_shell_reservation(
        account_id=account_id,
        platform_user_id="user_plum_test",
        reservation_idempotency_key="plum-test-reservation",
    )
    assert released_again["id"] == released["id"]
    assert db.get_wallet_balance_shell_micros(account_id=account_id) == 1_000_000_000

    with pytest.raises(db.FixedShellReservationReleased):
        db.reserve_fixed_shells(
            account_id=account_id,
            platform_user_id="user_plum_test",
            amount_shell_micros=5_000_000,
            idempotency_key="plum-test-reservation",
        )

    with pytest.raises(db.InsufficientWalletBalance):
        db.reserve_fixed_shells(
            account_id=account_id,
            platform_user_id="user_plum_test",
            amount_shell_micros=1_001_000_000,
            idempotency_key="plum-test-too-large",
        )


def test_plum_http_core_flow_charges_fixed_price_once(fresh_db, monkeypatch):
    """固定 API 串起 Feed/会话/turn，并保证 Runtime 不再二次按 token 扣费。"""

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()

    captured = {}

    def fake_run_product_turn(ctx, *, product_services):
        captured["ctx"] = ctx
        captured["services"] = product_services
        return SimpleNamespace(status="ok", reply="mock plum reply")

    monkeypatch.setattr(plum_api, "run_product_turn", fake_run_product_turn)
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(enabled=True),
    )

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/products/plum/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["wallet"]["balance"] == 1000

        feed = client.get("/api/v1/products/plum/feed")
        assert feed.status_code == 200
        assert len(feed.json()["items"]) == 10
        assert feed.json()["items"][0]["id"] == "char_ref_after_hours"

        created = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        )
        assert created.status_code == 200
        conversation = created.json()["conversation"]
        assert conversation["model_profile"] == "balanced"

        detail = client.get(
            f"/api/v1/products/plum/conversations/{conversation['id']}"
        )
        assert detail.status_code == 200
        experience = detail.json()["experience"]
        assert experience["profile"]["creator"]["display_name"] == "plum"
        assert len(experience["profile"]["hot_comments"]) == 2
        assert len(experience["profile"]["memories"]) == 3
        assert experience["viewer_state"] == {
            "relationship_level": 0,
            "relationship_xp": 0,
            "current_chapter": 1,
            "has_liked": False,
            "like_count": 119,
            "is_favorite": False,
            "favorite_count": 120,
        }
        assert experience["conversation_tools"]["role_card"]["id"] == (
            "fpersona_test_default"
        )

        liked = client.put(
            "/api/v1/products/plum/characters/char_ref_after_hours/like"
        )
        liked_again = client.put(
            "/api/v1/products/plum/characters/char_ref_after_hours/like"
        )
        assert liked.json() == {"status": "ok", "active": True, "count": 120}
        assert liked_again.json() == liked.json()
        unliked = client.delete(
            "/api/v1/products/plum/characters/char_ref_after_hours/like"
        )
        unliked_again = client.delete(
            "/api/v1/products/plum/characters/char_ref_after_hours/like"
        )
        assert unliked.json() == {"status": "ok", "active": False, "count": 119}
        assert unliked_again.json() == unliked.json()

        favorited = client.put(
            "/api/v1/products/plum/characters/char_ref_after_hours/favorite"
        )
        assert favorited.json() == {
            "status": "ok", "active": True, "count": 121
        }

        mismatched_ids = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            json={
                "text": "invalid idempotency pair",
                "client_message_id": "client-plum-mismatch",
                "idempotency_key": "turn-plum-mismatch",
            },
        )
        assert mismatched_ids.status_code == 422

        turn = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            json={
                "text": "hello plum",
                "client_message_id": "client-plum-1",
                "idempotency_key": "client-plum-1",
            },
        )
        assert turn.status_code == 200
        assert turn.json()["reply"]["text"] == "mock plum reply"
        assert turn.json()["charged_coins"] == 3
        assert turn.json()["wallet"]["balance"] == 997

        restarted = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/restart"
        )
        assert restarted.status_code == 200
        assert restarted.json()["conversation"]["id"] != conversation["id"]

    assert captured["services"].app_id == "plum"
    assert captured["ctx"].provider_id == "chatgpt"
    assert captured["ctx"].usage_billing_enabled is False


def test_plum_public_test_cookie_sessions_isolate_users(fresh_db, monkeypatch):
    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "production"
    config.plum_enabled = True
    config.plum_dev_mode = False
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)

    first_invite = repository.create_plum_access_invite(label="tester one")
    second_invite = repository.create_plum_access_invite(label="tester two")
    app = FastAPI()
    install_public_routes(app)

    with TestClient(app) as first_client:
        unauthenticated = first_client.get("/api/v1/products/plum/bootstrap")
        assert unauthenticated.status_code == 401

        login = first_client.post(
            "/api/v1/products/plum/auth/access-code",
            json={
                "access_code": first_invite["access_code"],
                "display_name": "Alice",
            },
        )
        assert login.status_code == 200
        first_user_id = login.json()["user"]["id"]
        assert login.json()["wallet"]["balance"] == 1000
        assert first_client.cookies.get("plum_session")
        csrf = first_client.cookies.get("plum_csrf")

        missing_csrf = first_client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        )
        assert missing_csrf.status_code == 403
        created = first_client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        )
        assert created.status_code == 200
        first_conversation_id = created.json()["conversation"]["id"]

    with TestClient(app) as second_client:
        login = second_client.post(
            "/api/v1/products/plum/auth/access-code",
            json={
                "access_code": second_invite["access_code"],
                "display_name": "Bob",
            },
        )
        assert login.status_code == 200
        assert login.json()["user"]["id"] != first_user_id
        assert login.json()["wallet"]["balance"] == 1000
        csrf = second_client.cookies.get("plum_csrf")
        created = second_client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        )
        assert created.status_code == 200
        assert created.json()["conversation"]["id"] != first_conversation_id

    with TestClient(app) as recovery_client:
        recovered = recovery_client.post(
            "/api/v1/products/plum/auth/access-code",
            json={
                "access_code": first_invite["access_code"],
                "display_name": "Alice",
            },
        )
        assert recovered.status_code == 200
        assert recovered.json()["user"]["id"] == first_user_id
        csrf = recovery_client.cookies.get("plum_csrf")
        logout = recovery_client.delete(
            "/api/v1/products/plum/auth/session/current",
            headers={"X-Plum-CSRF": csrf},
        )
        assert logout.status_code == 200
        assert recovery_client.get("/api/v1/products/plum/bootstrap").status_code == 401


def test_plum_public_test_rejects_unknown_access_code(fresh_db, monkeypatch):
    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "production"
    config.plum_enabled = True
    config.plum_dev_mode = False
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    app = FastAPI()
    install_public_routes(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/products/plum/auth/access-code",
            json={"access_code": "plum_unknown_access_code", "display_name": "Nope"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_access_code"


def test_plum_feature_flag_disables_public_login(fresh_db, monkeypatch):
    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "production"
    config.plum_enabled = False
    config.plum_dev_mode = False
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    invite = repository.create_plum_access_invite(label="disabled product")
    app = FastAPI()
    install_public_routes(app)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/products/plum/auth/access-code",
            json={
                "access_code": invite["access_code"],
                "display_name": "Disabled",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "plum_disabled"


def test_plum_rename_migration_preserves_legacy_rows(fresh_db, monkeypatch):
    """An already-used v66 database upgrades without losing product or owner data."""

    _configure_plum(monkeypatch, fresh_db)
    seeded = repository.seed_plum_dev()
    invite = repository.create_plum_access_invite(label="migration tester")

    with db.connect() as conn:
        conn.execute("ALTER TABLE plum_characters RENAME TO fibre_characters")
        conn.execute(
            "ALTER TABLE plum_access_invites RENAME TO fibre_access_invites"
        )
        conn.execute(
            "ALTER TABLE plum_character_memories "
            "RENAME COLUMN plum_conversation_id TO fibre_conversation_id"
        )
        conn.execute(
            "UPDATE accounts SET app_id='fibre' WHERE id=?",
            (seeded["entry_account_id"],),
        )
        conn.execute(
            "UPDATE product_memberships SET app_id='fibre' "
            "WHERE platform_user_id=?",
            (seeded["platform_user_id"],),
        )

        _migration_0067_plum_product_rename(conn)
        _migration_0067_plum_product_rename(conn)

        assert _table_exists(conn, "plum_characters")
        assert _table_exists(conn, "plum_access_invites")
        assert not _table_exists(conn, "fibre_characters")
        assert not _table_exists(conn, "fibre_access_invites")
        character = conn.execute(
            "SELECT display_name FROM plum_characters "
            "WHERE id='char_ref_after_hours'"
        ).fetchone()
        assert character["display_name"] == "Kai · After Hours"
        restored_invite = conn.execute(
            "SELECT label FROM plum_access_invites WHERE id=?",
            (invite["id"],),
        ).fetchone()
        assert restored_invite["label"] == "migration tester"
        columns = (
            {
                str(row["column_name"])
                for row in conn.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name='plum_character_memories'
                    """
                ).fetchall()
            }
            if db.is_postgres()
            else {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(plum_character_memories)"
                ).fetchall()
            }
        )
        assert "plum_conversation_id" in columns
        assert "fibre_conversation_id" not in columns
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id=?",
            (seeded["entry_account_id"],),
        ).fetchone()
        assert account["app_id"] == "plum"


def test_plum_rename_migration_refuses_coexisting_tables(fresh_db):
    with db.connect() as conn:
        conn.execute("CREATE TABLE fibre_characters (id TEXT PRIMARY KEY)")
        with pytest.raises(RuntimeError, match="coexisting product tables"):
            _migration_0067_plum_product_rename(conn)
