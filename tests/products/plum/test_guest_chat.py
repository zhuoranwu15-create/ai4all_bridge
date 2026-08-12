"""Guest-free conversation and server-authoritative action quota contracts."""
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.infrastructure import guest_repository, repository
from app.products.plum.manifest import install_public_routes
from app.agent_runtime.turns.service import RuntimeTurnEvent
from app.agent_runtime.turns import service as turn_service
from app.schemas import OpenClawTurnResponse

_GUEST_PROMPT_FRAGMENT = "Continue the scene naturally"


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
    config.plum_email_auth_enabled = False
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
    config.plum_chat_streaming_enabled = False
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    monkeypatch.setattr(guest_repository, "settings", config)
    monkeypatch.setattr(repository, "settings", config)
    monkeypatch.setattr(turn_service, "settings", config)
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


def test_guest_conversation_uses_free_model_and_no_wallet(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    with client:
        csrf, guest_id = _onboard(client)
        created = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        )
    assert created.status_code == 200
    conversation = created.json()["conversation"]
    assert conversation["model_profile"] == "guest_free"
    with db.connect() as conn:
        ownership = conn.execute(
            "SELECT owner_kind FROM runtime_ownerships WHERE runtime_account_id=?",
            (conversation["runtime_account_id"],),
        ).fetchone()
        assert ownership["owner_kind"] == "guest"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM entitlement_wallets WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM product_memberships WHERE platform_user_id=?",
            (guest_id,),
        ).fetchone()["n"] == 0


def test_guest_can_read_own_conversation_detail_and_history(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    with client:
        csrf, _ = _onboard(client)
        created = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        detail = client.get(
            f"/api/v1/products/plum/conversations/{created['id']}"
        )
        history = client.get("/api/v1/products/plum/conversations")
        context = client.get("/api/v1/products/plum/auth/context")

    assert detail.status_code == 200
    assert detail.json()["conversation"]["id"] == created["id"]
    assert detail.json()["models"] == []
    assert detail.json()["wallet"] is None
    assert detail.json()["guest_quota"]["typed_remaining"] == 2
    assert [item["id"] for item in history.json()["items"]] == [created["id"]]
    assert context.json()["capabilities"]["chat_streaming"] is False


def test_guest_typed_quota_is_global_and_idempotent(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)

    class Result:
        status = "ok"
        reply = "reply"

    monkeypatch.setattr(plum_api, "run_product_turn", lambda *args, **kwargs: Result())
    monkeypatch.setattr(
        plum_api,
        "get_duplicate_reply_record",
        lambda **kwargs: {"message_id": "reply-id", "content": "reply"},
    )
    with client:
        csrf, _ = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]

        def send(key):
            return client.post(
                f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
                headers={"X-Plum-CSRF": csrf},
                json={
                    "client_message_id": key,
                    "idempotency_key": key,
                    "action": {"kind": "message", "text": "hello"},
                },
            )

        first = send("guest-message-0001")
        replay = send("guest-message-0001")
        second = send("guest-message-0002")
        blocked = send("guest-message-0003")
        blocked_replay = send("guest-message-0003")

    assert first.status_code == replay.status_code == second.status_code == 200
    assert first.json()["wallet"] is None
    assert second.json()["guest_quota"]["typed_remaining"] == 0
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "guest_sign_in_required"
    assert blocked.json()["reason"] == "typed_limit"
    assert blocked_replay.status_code == 403
    assert blocked_replay.json()["reason"] == "typed_limit"


def test_continue_is_native_action_with_per_character_limit(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    seen = []

    class Result:
        status = "ok"
        reply = "continued"

    def run(ctx, **kwargs):
        seen.append(ctx)
        return Result()

    monkeypatch.setattr(plum_api, "run_product_turn", run)
    monkeypatch.setattr(
        plum_api,
        "get_duplicate_reply_record",
        lambda **kwargs: {"message_id": "reply-id", "content": "continued"},
    )
    with client:
        csrf, _ = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        responses = []
        for index in range(1, 4):
            responses.append(
                client.post(
                    f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
                    headers={"X-Plum-CSRF": csrf},
                    json={
                        "client_message_id": f"guest-continue-{index:04d}",
                        "idempotency_key": f"guest-continue-{index:04d}",
                        "action": {"kind": "continue"},
                    },
                )
            )
    assert [response.status_code for response in responses] == [200, 200, 403]
    assert responses[-1].json()["reason"] == "character_continue_limit"
    assert all(ctx.max_output_tokens == 384 for ctx in seen)
    assert all(ctx.persist_inbound_message is False for ctx in seen)
    assert all(ctx.raw == {"transport": "plum_web"} for ctx in seen)
    assert "one concrete question" not in seen[0].text
    assert "one concrete question" in seen[1].text


def test_guest_failed_action_can_retry_without_consuming_quota_twice(
    fresh_db, monkeypatch
):
    client = _client(monkeypatch, fresh_db)
    attempts = 0

    class Result:
        status = "ok"
        reply = "reply"

    def run(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return Result()

    monkeypatch.setattr(plum_api, "run_product_turn", run)
    monkeypatch.setattr(
        plum_api,
        "get_duplicate_reply_record",
        lambda **kwargs: {"message_id": "reply-id", "content": "reply"},
    )
    with client:
        csrf, guest_id = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        payload = {
            "client_message_id": "guest-retry-0001",
            "idempotency_key": "guest-retry-0001",
            "action": {"kind": "message", "text": "hello"},
        }
        first = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            headers={"X-Plum-CSRF": csrf},
            json=payload,
        )
        retry = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            headers={"X-Plum-CSRF": csrf},
            json=payload,
        )

    assert first.status_code == 502
    assert retry.status_code == 200
    assert retry.json()["guest_quota"]["typed_remaining"] == 1
    with db.connect() as conn:
        receipt = conn.execute(
            """
            SELECT status FROM plum_guest_action_receipts
            WHERE platform_user_id=? AND client_action_id=?
            """,
            (guest_id, "guest-retry-0001"),
        ).fetchone()
    assert receipt["status"] == "completed"


def test_guest_provider_precheck_does_not_consume_quota(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: type(
            "Provider", (), {"id": "deepseek", "model": "test", "enabled": False}
        )(),
    )
    with client:
        csrf, guest_id = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        response = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            headers={"X-Plum-CSRF": csrf},
            json={
                "client_message_id": "guest-provider-0001",
                "idempotency_key": "guest-provider-0001",
                "action": {"kind": "message", "text": "hello"},
            },
        )

    assert response.status_code == 503
    assert guest_repository.get_guest_quota(
        platform_user_id=guest_id
    )["typed_remaining"] == 2


def test_continue_runtime_persists_assistant_only(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    monkeypatch.setattr(
        turn_service,
        "generate_reply_with_tools",
        lambda **kwargs: ("The scene moves forward.", None),
    )
    with client:
        csrf, _ = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        response = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            headers={"X-Plum-CSRF": csrf},
            json={
                "client_message_id": "guest-native-continue-0001",
                "idempotency_key": "guest-native-continue-0001",
                "action": {"kind": "continue"},
            },
        )
        replay = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns",
            headers={"X-Plum-CSRF": csrf},
            json={
                "client_message_id": "guest-native-continue-0001",
                "idempotency_key": "guest-native-continue-0001",
                "action": {"kind": "continue"},
            },
        )

    assert response.status_code == 200
    assert response.json()["reply"]["text"] == "The scene moves forward."
    assert replay.status_code == 200
    assert replay.json()["deduplicated"] is True
    assert replay.json()["reply"] == response.json()["reply"]
    rows = db.list_recent_messages_for_account(
        account_id=conversation["runtime_account_id"], limit=10
    )
    assert [(row["role"], row["content"]) for row in rows] == [
        ("assistant", "The scene moves forward.")
    ]
    assert response.json()["reply"]["message_id"] == rows[0]["message_id"]
    assert _GUEST_PROMPT_FRAGMENT not in str(rows)


def test_guest_stream_returns_authoritative_quota(fresh_db, monkeypatch):
    client = _client(monkeypatch, fresh_db)
    seen = []

    def stream(ctx, **kwargs):
        seen.append(ctx)
        yield RuntimeTurnEvent(kind="accepted", turn_id=ctx.turn_id)
        yield RuntimeTurnEvent(kind="text_delta", turn_id=ctx.turn_id, seq=1, text="hi")
        yield RuntimeTurnEvent(
            kind="completed",
            turn_id=ctx.turn_id,
            response=OpenClawTurnResponse(
                status="ok", reply="hi", metadata={"reply_message_id": "guest-sse-reply"}
            ),
        )

    monkeypatch.setattr(plum_api, "run_product_turn_stream", stream)
    monkeypatch.setattr(
        plum_api,
        "get_llm_provider",
        lambda *_args, **_kwargs: type(
            "Provider", (), {"id": "deepseek", "model": "test", "enabled": True}
        )(),
    )
    with client:
        csrf, _ = _onboard(client)
        conversation = client.post(
            "/api/v1/products/plum/conversations",
            headers={"X-Plum-CSRF": csrf},
            json={"character_id": "char_ref_after_hours"},
        ).json()["conversation"]
        plum_api.settings.plum_chat_streaming_enabled = True
        response = client.post(
            f"/api/v1/products/plum/conversations/{conversation['id']}/turns/stream",
            headers={"X-Plum-CSRF": csrf},
            json={
                "client_message_id": "guest-sse-message-0001",
                "idempotency_key": "guest-sse-message-0001",
                "action": {"kind": "message", "text": "hello"},
            },
        )
    assert response.status_code == 200
    assert '"typed_remaining":1' in response.text
    assert '"wallet":null' in response.text
    assert seen[0].max_output_tokens == 384
