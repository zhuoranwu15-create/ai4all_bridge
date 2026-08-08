"""Fibre MVP 的产品隔离、会话和固定金币用例。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.products.fibre.api import app as fibre_api
from app.products.fibre.api import deps as fibre_deps
from app.products.fibre.infrastructure import repository
from app.products.fibre.manifest import install_public_routes


def _configure_fibre(monkeypatch, fresh_db):
    config = MagicMock(wraps=fresh_db)
    config.fibre_test_user_id = "user_fibre_test"
    config.fibre_test_phone = "fibre-test@local.invalid"
    config.fibre_fast_provider_id = "deepseek"
    config.fibre_balanced_provider_id = "chatgpt"
    config.fibre_immersive_provider_id = "deepseek-v4-pro"
    monkeypatch.setattr(repository, "settings", config)
    return config


def test_fibre_seed_is_idempotent_and_product_scoped(fresh_db, monkeypatch):
    _configure_fibre(monkeypatch, fresh_db)

    first = repository.seed_fibre_dev()
    second = repository.seed_fibre_dev()

    assert first["platform_user_id"] == "user_fibre_test"
    assert second["grant_balance_shells"] == "1000"
    characters = repository.list_characters()
    assert len(characters) == 10
    assert characters[0]["id"] == "char_ref_after_hours"
    assert characters[0]["creator"]["display_name"] == "fibre"
    assert characters[0]["interaction_count"] == 48200
    assert characters[0]["capabilities"] == {"text": True, "voice": True}
    assert characters[0]["badges"][0]["code"] == "featured"
    assert [item["profile"] for item in repository.list_model_profiles()] == [
        "fast",
        "balanced",
        "immersive",
    ]
    membership = db.get_product_membership(
        platform_user_id="user_fibre_test", app_id="fibre"
    )
    assert membership["status"] == "active"
    wallet = db.get_wallet_summary(account_id=first["entry_account_id"])
    assert wallet["wallet"]["balance_shell_micros"] == 1_000_000_000


def test_fibre_conversation_uses_shared_runtime_account_and_session(
    fresh_db, monkeypatch
):
    _configure_fibre(monkeypatch, fresh_db)
    repository.seed_fibre_dev()

    first = repository.create_or_get_conversation(
        platform_user_id="user_fibre_test", character_id="char_ref_after_hours"
    )
    replay = repository.create_or_get_conversation(
        platform_user_id="user_fibre_test", character_id="char_ref_after_hours"
    )

    assert replay["id"] == first["id"]
    assert first["model_profile"] == "balanced"
    assert first["character"]["display_name"] == "Kai · After Hours"
    assert repository.list_conversation_messages(first) == []
    with db.connect() as conn:
        assert (
            db.resolve_owner_platform_user_id(conn, first["runtime_account_id"])
            == "user_fibre_test"
        )
        account = conn.execute(
            "SELECT app_id FROM accounts WHERE id=?",
            (first["runtime_account_id"],),
        ).fetchone()
        assert account["app_id"] == "fibre"


def test_fixed_wallet_reservation_prevents_negative_balance_and_releases(
    fresh_db, monkeypatch
):
    _configure_fibre(monkeypatch, fresh_db)
    seeded = repository.seed_fibre_dev()
    account_id = seeded["entry_account_id"]

    first = db.reserve_fixed_shells(
        account_id=account_id,
        platform_user_id="user_fibre_test",
        amount_shell_micros=5_000_000,
        idempotency_key="fibre-test-reservation",
    )
    replay = db.reserve_fixed_shells(
        account_id=account_id,
        platform_user_id="user_fibre_test",
        amount_shell_micros=5_000_000,
        idempotency_key="fibre-test-reservation",
    )
    assert replay["id"] == first["id"]
    assert db.get_wallet_balance_shell_micros(account_id=account_id) == 995_000_000

    released = db.release_fixed_shell_reservation(
        account_id=account_id,
        platform_user_id="user_fibre_test",
        reservation_idempotency_key="fibre-test-reservation",
    )
    released_again = db.release_fixed_shell_reservation(
        account_id=account_id,
        platform_user_id="user_fibre_test",
        reservation_idempotency_key="fibre-test-reservation",
    )
    assert released_again["id"] == released["id"]
    assert db.get_wallet_balance_shell_micros(account_id=account_id) == 1_000_000_000

    with pytest.raises(db.FixedShellReservationReleased):
        db.reserve_fixed_shells(
            account_id=account_id,
            platform_user_id="user_fibre_test",
            amount_shell_micros=5_000_000,
            idempotency_key="fibre-test-reservation",
        )

    with pytest.raises(db.InsufficientWalletBalance):
        db.reserve_fixed_shells(
            account_id=account_id,
            platform_user_id="user_fibre_test",
            amount_shell_micros=1_001_000_000,
            idempotency_key="fibre-test-too-large",
        )


def test_fibre_http_core_flow_charges_fixed_price_once(fresh_db, monkeypatch):
    """固定 API 串起 Feed/会话/turn，并保证 Runtime 不再二次按 token 扣费。"""

    config = _configure_fibre(monkeypatch, fresh_db)
    config.app_env = "test"
    config.fibre_enabled = True
    config.fibre_dev_mode = True
    monkeypatch.setattr(fibre_api, "settings", config)
    monkeypatch.setattr(fibre_deps, "settings", config)
    repository.seed_fibre_dev()

    captured = {}

    def fake_run_product_turn(ctx, *, product_services):
        captured["ctx"] = ctx
        captured["services"] = product_services
        return SimpleNamespace(status="ok", reply="mock fibre reply")

    monkeypatch.setattr(fibre_api, "run_product_turn", fake_run_product_turn)
    monkeypatch.setattr(
        fibre_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(enabled=True),
    )

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/products/fibre/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["wallet"]["balance"] == 1000

        feed = client.get("/api/v1/products/fibre/feed")
        assert feed.status_code == 200
        assert len(feed.json()["items"]) == 10
        assert feed.json()["items"][0]["id"] == "char_ref_after_hours"

        created = client.post(
            "/api/v1/products/fibre/conversations",
            json={"character_id": "char_ref_after_hours"},
        )
        assert created.status_code == 200
        conversation = created.json()["conversation"]
        assert conversation["model_profile"] == "balanced"

        detail = client.get(
            f"/api/v1/products/fibre/conversations/{conversation['id']}"
        )
        assert detail.status_code == 200
        experience = detail.json()["experience"]
        assert experience["profile"]["creator"]["display_name"] == "fibre"
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
            "/api/v1/products/fibre/characters/char_ref_after_hours/like"
        )
        liked_again = client.put(
            "/api/v1/products/fibre/characters/char_ref_after_hours/like"
        )
        assert liked.json() == {"status": "ok", "active": True, "count": 120}
        assert liked_again.json() == liked.json()
        unliked = client.delete(
            "/api/v1/products/fibre/characters/char_ref_after_hours/like"
        )
        unliked_again = client.delete(
            "/api/v1/products/fibre/characters/char_ref_after_hours/like"
        )
        assert unliked.json() == {"status": "ok", "active": False, "count": 119}
        assert unliked_again.json() == unliked.json()

        favorited = client.put(
            "/api/v1/products/fibre/characters/char_ref_after_hours/favorite"
        )
        assert favorited.json() == {
            "status": "ok", "active": True, "count": 121
        }

        mismatched_ids = client.post(
            f"/api/v1/products/fibre/conversations/{conversation['id']}/turns",
            json={
                "text": "invalid idempotency pair",
                "client_message_id": "client-fibre-mismatch",
                "idempotency_key": "turn-fibre-mismatch",
            },
        )
        assert mismatched_ids.status_code == 422

        turn = client.post(
            f"/api/v1/products/fibre/conversations/{conversation['id']}/turns",
            json={
                "text": "hello fibre",
                "client_message_id": "client-fibre-1",
                "idempotency_key": "client-fibre-1",
            },
        )
        assert turn.status_code == 200
        assert turn.json()["reply"]["text"] == "mock fibre reply"
        assert turn.json()["charged_coins"] == 3
        assert turn.json()["wallet"]["balance"] == 997

        restarted = client.post(
            f"/api/v1/products/fibre/conversations/{conversation['id']}/restart"
        )
        assert restarted.status_code == 200
        assert restarted.json()["conversation"]["id"] != conversation["id"]

    assert captured["services"].app_id == "fibre"
    assert captured["ctx"].provider_id == "chatgpt"
    assert captured["ctx"].usage_billing_enabled is False
