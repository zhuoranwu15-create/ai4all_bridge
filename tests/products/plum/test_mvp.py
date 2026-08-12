"""Plum MVP 的产品隔离、会话和固定金币用例。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.schemas import OpenClawTurnResponse
from app.db._core import _migration_0067_plum_product_rename, _table_exists
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.infrastructure import repository
from app.products.plum.infrastructure import guest_repository
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
    config.plum_chat_streaming_enabled = False
    config.plum_guest_chat_enabled = False
    config.plum_email_auth_enabled = False
    config.plum_google_auth_enabled = False
    config.plum_apple_auth_enabled = False
    config.plum_guest_session_cookie_name = "plum_guest_session"
    config.plum_guest_session_days = 30
    config.plum_guest_typed_limit = 2
    config.plum_guest_continue_limit = 8
    config.plum_guest_character_continue_limit = 2
    monkeypatch.setattr(repository, "settings", config)
    monkeypatch.setattr(guest_repository, "settings", config)
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
    with db.connect() as conn:
        foundation = conn.execute(
            """
            SELECT COUNT(DISTINCT ch.work_id) AS work_count,
                   COUNT(DISTINCT cv.character_id) AS versioned_count,
                   COUNT(*) FILTER (WHERE cv.version_number=1) AS v1_count
            FROM plum_characters ch
            JOIN plum_character_versions cv
              ON cv.character_id=ch.id AND cv.version_number=ch.content_version
            WHERE ch.fixture_version=?
            """,
            (repository.FIXTURE_VERSION,),
        ).fetchone()
        assert foundation["work_count"] == 10
        assert foundation["versioned_count"] == 10
        assert foundation["v1_count"] == 10
    wallet = db.get_wallet_summary(account_id=first["entry_account_id"])
    assert wallet["wallet"]["balance_shell_micros"] == 1_000_000_000


def test_plum_seed_rejects_silent_builtin_fixture_drift(fresh_db, monkeypatch):
    """内置内容版本变化必须走发布路径，seed 不得静默保留或覆盖旧内容。"""

    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE plum_characters SET fixture_version='reference_fixture_old'
            WHERE id='char_ref_after_hours'
            """
        )

    with pytest.raises(RuntimeError, match="fixture version drift"):
        repository.seed_plum_catalog()


def test_plum_feed_is_public_without_session(fresh_db, monkeypatch):
    """Visitors can browse characters while account-scoped APIs stay protected."""

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = False
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        feed = client.get("/api/v1/products/plum/feed")
        assert feed.status_code == 200
        assert len(feed.json()["items"]) == 10
        assert client.get("/api/v1/products/plum/bootstrap").status_code == 401


def test_plum_feed_respects_product_disabled_flag(fresh_db, monkeypatch):
    """Public browsing must not bypass the product-level kill switch."""

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = False
    config.plum_dev_mode = False
    monkeypatch.setattr(plum_deps, "settings", config)

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        feed = client.get("/api/v1/products/plum/feed")

    assert feed.status_code == 503
    assert feed.json()["detail"] == "plum_disabled"


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


def test_plum_conversation_restores_cancelled_assistant_status(
    fresh_db, monkeypatch
):
    _configure_plum(monkeypatch, fresh_db)
    repository.seed_plum_dev()
    conversation = repository.create_or_get_conversation(
        platform_user_id="user_plum_test", character_id="char_ref_after_hours"
    )
    account_id = conversation["runtime_account_id"]
    session_id = conversation["runtime_session_id"]

    db.insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id="assistant-cancelled",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content="partial reply",
    )
    db.insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id="assistant-completed",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content="ordinary reply",
    )
    db.create_runtime_turn_run(
        turn_id="turn-cancelled-refresh",
        app_id="plum",
        account_id=account_id,
        session_id=session_id,
        client_message_id="client-cancelled-refresh",
        idempotency_key="client-cancelled-refresh",
        provider_id="chatgpt",
        model_ref="gpt-test",
    )
    db.finish_runtime_turn_run(
        turn_id="turn-cancelled-refresh",
        status="cancelled",
        assistant_message_id="assistant-cancelled",
        finish_reason="cancelled",
    )

    messages = {
        message["message_id"]: message
        for message in repository.list_conversation_messages(conversation)
    }
    assert messages["assistant-cancelled"]["status"] == "cancelled"
    assert messages["assistant-completed"]["status"] == "completed"


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

        history = client.get("/api/v1/products/plum/conversations")
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [
            conversation["id"]
        ]
        assert history.json()["items"][0]["character"]["id"] == (
            "char_ref_after_hours"
        )

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
            f"/api/v1/products/plum/conversations/{conversation['id']}/restart",
            json={"idempotency_key": "restart-plum-1"},
        )
        assert restarted.status_code == 200
        assert restarted.json()["conversation"]["id"] != conversation["id"]

    assert captured["services"].app_id == "plum"
    assert captured["ctx"].provider_id == "chatgpt"
    assert captured["ctx"].usage_billing_enabled is False


def test_plum_streaming_feature_flag_and_completed_billing(fresh_db, monkeypatch):
    from app.agent_runtime.turns.service import RuntimeTurnEvent

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chatgpt", model="gpt-test", enabled=True
        ),
    )

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/products/plum/bootstrap")
        assert bootstrap.json()["capabilities"] == {"chat_streaming": False}
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        disabled = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "hello",
                "client_message_id": "client-stream-disabled",
                "idempotency_key": "client-stream-disabled",
            },
        )
        assert disabled.status_code == 503

        config.plum_chat_streaming_enabled = True

        def fake_stream(ctx, *, product_services, cancellation):
            del product_services, cancellation
            yield RuntimeTurnEvent(kind="accepted", turn_id=ctx.turn_id)
            yield RuntimeTurnEvent(kind="text_delta", turn_id=ctx.turn_id, seq=1, text="你")
            yield RuntimeTurnEvent(kind="text_delta", turn_id=ctx.turn_id, seq=2, text="好")
            yield RuntimeTurnEvent(
                kind="completed",
                turn_id=ctx.turn_id,
                response=OpenClawTurnResponse(
                    status="ok",
                    reply="你好",
                    metadata={"reply_message_id": "reply-stream-1"},
                ),
            )

        monkeypatch.setattr(plum_api, "run_product_turn_stream", fake_stream)
        streamed = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "hello streaming",
                "client_message_id": "client-stream-1",
                "idempotency_key": "client-stream-1",
            },
        )

        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert streamed.headers["x-accel-buffering"] == "no"
        assert [
            line.removeprefix("event: ")
            for line in streamed.text.splitlines()
            if line.startswith("event: ")
        ] == ["turn.accepted", "message.delta", "message.delta", "turn.completed"]
        assert client.get("/api/v1/products/plum/bootstrap").json()["wallet"]["balance"] == 997


def test_plum_streaming_failure_releases_fixed_coins(fresh_db, monkeypatch):
    from app.agent_runtime.turns.service import RuntimeTurnEvent

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    config.plum_chat_streaming_enabled = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chatgpt", model="gpt-test", enabled=True
        ),
    )

    def fake_stream(ctx, *, product_services, cancellation):
        del product_services, cancellation
        yield RuntimeTurnEvent(kind="accepted", turn_id=ctx.turn_id)
        yield RuntimeTurnEvent(
            kind="failed", turn_id=ctx.turn_id, error_code="provider_failed"
        )

    monkeypatch.setattr(plum_api, "run_product_turn_stream", fake_stream)
    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        streamed = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "fail streaming",
                "client_message_id": "client-stream-fail",
                "idempotency_key": "client-stream-fail",
            },
        )
        assert "event: turn.failed" in streamed.text
        assert client.get("/api/v1/products/plum/bootstrap").json()["wallet"]["balance"] == 1000


def test_plum_streaming_exception_finishes_run_and_releases_coins(
    fresh_db, monkeypatch
):
    from app.agent_runtime.turns.service import RuntimeTurnEvent

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    config.plum_chat_streaming_enabled = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chatgpt", model="gpt-test", enabled=True
        ),
    )

    def exploding_stream(ctx, *, product_services, cancellation):
        del product_services, cancellation
        yield RuntimeTurnEvent(kind="accepted", turn_id=ctx.turn_id)
        raise RuntimeError("synthetic runtime failure")

    monkeypatch.setattr(plum_api, "run_product_turn_stream", exploding_stream)
    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        streamed = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "explode streaming",
                "client_message_id": "client-stream-explode",
                "idempotency_key": "client-stream-explode",
            },
        )

        assert "event: turn.failed" in streamed.text
        assert client.get("/api/v1/products/plum/bootstrap").json()["wallet"]["balance"] == 1000
        run = db.get_runtime_turn_run_by_idempotency(
            app_id="plum",
            account_id=conversation["runtime_account_id"],
            idempotency_key="client-stream-explode",
        )
        assert run["status"] == "failed"
        assert run["error_code"] == "stream_internal_error"


def test_plum_streaming_reclaims_stale_unseen_run_and_refunds_reservation(
    fresh_db, monkeypatch
):
    from app.agent_runtime.turns.service import RuntimeTurnEvent

    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    config.plum_chat_streaming_enabled = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chatgpt", model="gpt-test", enabled=True
        ),
    )

    def completed_stream(ctx, *, product_services, cancellation):
        del product_services, cancellation
        yield RuntimeTurnEvent(kind="accepted", turn_id=ctx.turn_id)
        yield RuntimeTurnEvent(kind="text_delta", turn_id=ctx.turn_id, seq=1, text="ok")
        yield RuntimeTurnEvent(
            kind="completed",
            turn_id=ctx.turn_id,
            response=OpenClawTurnResponse(
                status="ok",
                reply="ok",
                metadata={"reply_message_id": "reply-after-stale"},
            ),
        )

    monkeypatch.setattr(plum_api, "run_product_turn_stream", completed_stream)
    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        stale_key = "client-stream-stale"
        db.reserve_fixed_shells(
            account_id=conversation["runtime_account_id"],
            platform_user_id="user_plum_test",
            amount_shell_micros=3_000_000,
            idempotency_key=f"plum-turn:{conversation['id']}:{stale_key}",
            source_id=conversation["id"],
        )
        db.create_runtime_turn_run(
            turn_id="turn-stream-stale",
            app_id="plum",
            account_id=conversation["runtime_account_id"],
            session_id=conversation["runtime_session_id"],
            client_message_id=stale_key,
            idempotency_key=stale_key,
            provider_id="chatgpt",
            model_ref="gpt-test",
        )
        with db.connect() as conn:
            conn.execute(
                "UPDATE runtime_turn_runs SET updated_at='2000-01-01 00:00:00' "
                "WHERE id='turn-stream-stale'"
            )

        streamed = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "after stale",
                "client_message_id": "client-stream-after-stale",
                "idempotency_key": "client-stream-after-stale",
            },
        )

        assert "event: turn.completed" in streamed.text
        assert db.get_runtime_turn_run("turn-stream-stale")["status"] == "abandoned"
        assert client.get("/api/v1/products/plum/bootstrap").json()["wallet"]["balance"] == 997


def test_plum_streaming_rejects_active_conversation_and_releases_coins(
    fresh_db, monkeypatch
):
    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    config.plum_chat_streaming_enabled = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="chatgpt", model="gpt-test", enabled=True
        ),
    )

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        db.create_runtime_turn_run(
            turn_id="turn-already-active",
            app_id="plum",
            account_id=conversation["runtime_account_id"],
            session_id=conversation["runtime_session_id"],
            client_message_id="client-already-active",
            idempotency_key="client-already-active",
            provider_id="chatgpt",
            model_ref="gpt-test",
        )

        blocked = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            json={
                "text": "second concurrent turn",
                "client_message_id": "client-concurrent-second",
                "idempotency_key": "client-concurrent-second",
            },
        )

        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "conversation_turn_active"
        assert client.get("/api/v1/products/plum/bootstrap").json()["wallet"]["balance"] == 1000
        assert db.get_runtime_turn_run_by_idempotency(
            app_id="plum",
            account_id=conversation["runtime_account_id"],
            idempotency_key="client-concurrent-second",
        ) is None


def test_plum_stream_cancel_endpoint_requests_scoped_runtime_cancel(fresh_db, monkeypatch):
    config = _configure_plum(monkeypatch, fresh_db)
    config.app_env = "test"
    config.plum_enabled = True
    config.plum_dev_mode = True
    config.plum_chat_streaming_enabled = True
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    repository.seed_plum_dev()

    app = FastAPI()
    install_public_routes(app)
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        db.create_runtime_turn_run(
            turn_id="turn-cancel-api",
            app_id="plum",
            account_id=conversation["runtime_account_id"],
            session_id=conversation["runtime_session_id"],
            client_message_id="client-cancel-api",
            idempotency_key="client-cancel-api",
            provider_id="chatgpt",
            model_ref="gpt-test",
        )

        cancelled = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}"
            "/turns/client-cancel-api/cancel"
        )

        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert cancelled.json()["run_status"] == "accepted"
        assert db.get_runtime_turn_run("turn-cancel-api")["cancel_requested_at"] is not None


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
        first_conversation = created.json()["conversation"]
        first_conversation_id = first_conversation["id"]

    db.create_runtime_turn_run(
        turn_id="turn-first-user-active",
        app_id="plum",
        account_id=first_conversation["runtime_account_id"],
        session_id=first_conversation["runtime_session_id"],
        client_message_id="client-first-user-active",
        idempotency_key="client-first-user-active",
        provider_id="chatgpt",
        model_ref="gpt-test",
    )

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
        forbidden_cancel = second_client.post(
            f"/api/v1/products/plum/conversations/{first_conversation_id}"
            "/turns/client-first-user-active/cancel",
            headers={"X-Plum-CSRF": csrf},
        )
        assert forbidden_cancel.status_code == 404
        assert (
            db.get_runtime_turn_run("turn-first-user-active")["cancel_requested_at"]
            is None
        )

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
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='plum_character_memories'
                """
            ).fetchall()
        }
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
