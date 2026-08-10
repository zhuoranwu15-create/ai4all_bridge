"""鸣蝉居民会话 API 的 audience、owner、产品归属与幂等边界。"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    ZHAOXI_APP_ID,
    build_test_product_registry,
)
from app.products.mingchan.application.identity import create_mingchan_login_session
from app.products.mingchan.domain.companion_world import (
    MingchanWorldOnboardingService,
    ResidentSelection,
)
from app.products.mingchan.infrastructure.world_onboarding import (
    SqlMingchanWorldOnboardingRepository,
)
from app.products.mingchan.manifest import install_public_routes


def _verified_token(phone: str) -> str:
    row = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    return str(
        db.set_verification_verified(row["id"], token_expires_minutes=10)[
            "verified_token"
        ]
    )


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            app_id=MINGCHAN_APP_ID,
            template_id=f"tmpl_mingchan_conversation_{rank}",
            source_type="operations",
            name=f"鸣蝉会话居民{rank}",
            avatar_ref=f"asset://mingchan-conversation-{rank}",
            summary=f"鸣蝉会话简介{rank}",
            tags_json=json.dumps(["鸣蝉", "会话", str(rank)], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n鸣蝉会话人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 名字：鸣蝉会话居民{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
            persona_key="linxiaoman" if rank == 1 else None,
        )


def _scope(config, phone: str) -> tuple[TestClient, dict, dict, object]:
    registry = build_test_product_registry()
    login = create_mingchan_login_session(
        phone=phone,
        verified_token=_verified_token(phone),
        registry=registry,
    )
    user_id = str(login["registration"]["platform_user"]["id"])
    onboarding = MingchanWorldOnboardingService(
        SqlMingchanWorldOnboardingRepository(registry=registry)
    )
    candidate = onboarding.bootstrap_home(user_id).candidates[0]
    resident = onboarding.confirm_residents(
        user_id,
        [ResidentSelection(template_id=candidate.template.id)],
    )[0]
    app = FastAPI()
    install_public_routes(app, registry=registry, config=config)
    headers = {"Authorization": f"Bearer {login['session']['token']}"}
    return (
        TestClient(app),
        headers,
        {
            "platform_user_id": user_id,
            "conversation_id": resident.conversation_id,
            "runtime_account_id": resident.runtime_account_id,
        },
        registry,
    )


def test_mingchan_conversation_list_history_read_and_turn_are_self_contained(
    fresh_db,
    monkeypatch,
):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, scope, _registry = _scope(fresh_db, "13800037940")

    monkeypatch.setattr("app.agent_runtime.turns.service.settings", fresh_db)
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.generate_reply_with_tools",
        lambda **_: ("鸣蝉会话回复", None),
    )
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.record_chat_usage_charge",
        lambda **_: None,
    )

    listing = client.get(
        "/api/v1/products/mingchan/conversations",
        headers=headers,
    )
    history = client.get(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/messages",
        headers=headers,
    )
    body = {"client_message_id": "mingchan_turn_001", "text": "你好"}
    first = client.post(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/turn",
        headers=headers,
        json=body,
    )
    replay = client.post(
        f"/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/turn",
        headers=headers,
        json=body,
    )

    assert listing.status_code == history.status_code == 200
    assert listing.json()["data"]["items"][0]["conversation_id"] == scope[
        "conversation_id"
    ]
    assert scope["runtime_account_id"] not in listing.text
    assert history.json()["data"]["messages"][0]["role"] == "assistant"
    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["reply"]["text"] == "鸣蝉会话回复"
    assert replay.json()["data"]["deduplicated"] is True
    assert replay.json()["data"]["reply"]["message_id"] == first.json()["data"][
        "reply"
    ]["message_id"]

    updated_history = client.get(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/messages",
        headers=headers,
    ).json()["data"]["messages"]
    assert [item["text"] for item in updated_history[-2:]] == [
        "你好",
        "鸣蝉会话回复",
    ]
    last_message_id = updated_history[-1]["id"]
    read = client.post(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/read",
        headers=headers,
        json={"last_message_id": last_message_id},
    )
    assert read.status_code == 200
    assert read.json()["data"] == {
        "conversation_id": scope["conversation_id"],
        "last_read_message_id": last_message_id,
        "unread": 0,
    }
    with db.connect() as conn:
        inbound_count = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE account_id = ? AND message_id = ?",
            (
                scope["runtime_account_id"],
                f"app:{scope['conversation_id']}:mingchan_turn_001",
            ),
        ).fetchone()["n"]
    assert inbound_count == 1


def test_mingchan_conversation_rejects_cross_owner_and_zhaoxi_audience(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, scope, registry = _scope(fresh_db, "13800037941")
    other = db.create_or_get_platform_user_by_phone(phone="13800037942")
    db.ensure_product_membership(
        platform_user_id=other["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    other_session = db.create_platform_user_session(
        platform_user_id=other["id"],
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    db.ensure_product_membership(
        platform_user_id=scope["platform_user_id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    zhaoxi_session = db.create_platform_user_session(
        platform_user_id=scope["platform_user_id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    other_headers = {"Authorization": f"Bearer {other_session['token']}"}
    zhaoxi_headers = {"Authorization": f"Bearer {zhaoxi_session['token']}"}

    denied = client.get(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/messages",
        headers=other_headers,
    )
    wrong_audience = client.get(
        "/api/v1/products/mingchan/conversations",
        headers=zhaoxi_headers,
    )

    assert denied.status_code == 404
    assert denied.json()["code"] == "conversation_not_found"
    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["code"] == "unauthorized"
    assert client.get(
        "/api/v1/products/mingchan/conversations", headers=headers
    ).status_code == 200


def test_mingchan_conversation_fails_closed_on_runtime_product_mismatch(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, scope, _registry = _scope(fresh_db, "13800037943")
    with db.connect() as conn:
        conn.execute(
            "UPDATE accounts SET app_id = ? WHERE id = ?",
            (ZHAOXI_APP_ID, scope["runtime_account_id"]),
        )

    listing = client.get(
        "/api/v1/products/mingchan/conversations",
        headers=headers,
    )
    history = client.get(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/messages",
        headers=headers,
    )
    turn = client.post(
        f"/api/v1/products/mingchan/ai-conversations/"
        f"{scope['conversation_id']}/turn",
        headers=headers,
        json={"client_message_id": "mismatch_turn_01", "text": "你好"},
    )

    assert listing.status_code == 200
    assert listing.json()["data"]["items"] == []
    assert history.status_code == turn.status_code == 404
    assert history.json()["code"] == turn.json()["code"] == "conversation_not_found"


def test_mingchan_conversation_openapi_is_canonical_and_text_only(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    registry = build_test_product_registry()
    app = FastAPI()
    install_public_routes(app, registry=registry, config=fresh_db)
    client = TestClient(app)

    paths = set(client.get("/openapi.json").json()["paths"])

    assert "/api/v1/products/mingchan/conversations" in paths
    assert (
        "/api/v1/products/mingchan/ai-conversations/{conversation_id}/turn"
        in paths
    )
    assert not any(path.startswith("/v1/products/mingchan") for path in paths)
