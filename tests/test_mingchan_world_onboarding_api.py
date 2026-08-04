"""鸣蝉 World onboarding 的产品归属与 HTTP 边界。"""
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
from app.products.mingchan.domain.companion_world.onboarding_content import (
    RESIDENT_INTRO_CONTENT,
)
from app.products.mingchan.manifest import install_public_routes


def _seed_catalog() -> None:
    persona_keys = ("linxiaoman", "luxingye", "shenchuan", "atang")
    for rank in range(1, 5):
        db.create_character_template(
            app_id=MINGCHAN_APP_ID,
            template_id=f"tmpl_mingchan_{rank}",
            source_type="operations",
            name=f"鸣蝉角色{rank}",
            avatar_ref=f"asset://mingchan-avatar-{rank}",
            summary=f"鸣蝉简介{rank}",
            tags_json=json.dumps(
                ["温柔", "好奇", f"类型{rank}"],
                ensure_ascii=False,
            ),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n鸣蝉人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是鸣蝉角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
            persona_key=persona_keys[rank - 1],
        )


def _verified_token(phone: str) -> str:
    verification = db.create_phone_verification(
        phone=phone,
        code="999999",
        expires_minutes=10,
    )
    verified = db.set_verification_verified(
        verification["id"],
        token_expires_minutes=10,
    )
    return str(verified["verified_token"])


def _client_and_login(config, phone: str) -> tuple[TestClient, dict, object]:
    registry = build_test_product_registry()
    app = FastAPI()
    install_public_routes(app, registry=registry, config=config)
    login = create_mingchan_login_session(
        phone=phone,
        verified_token=_verified_token(phone),
        registry=registry,
    )
    headers = {"Authorization": f"Bearer {login['session']['token']}"}
    return TestClient(app), headers, registry


def test_mingchan_bootstrap_is_idempotent_and_hides_persona(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, _registry = _client_and_login(fresh_db, "13800037921")

    first = client.post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers=headers,
    )
    second = client.post(
        "/v1/products/mingchan/worlds/home/bootstrap",
        headers=headers,
    )
    listing = client.get(
        "/api/v1/products/mingchan/worlds/home/resident-candidates",
        headers=headers,
    )

    assert first.status_code == second.status_code == listing.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["world"]["onboarding_state"] == "selecting"
    assert len(first_data["candidates"]) == 4
    assert first_data["existing_residents"] == []
    assert second_data["candidates"] == first_data["candidates"]
    assert listing.json()["data"]["candidates"] == first_data["candidates"]
    assert "persona_seed_json" not in first.text
    assert "鸣蝉人格1" not in first.text
    with db.connect() as conn:
        world = conn.execute("SELECT app_id FROM universes").fetchone()
    assert world["app_id"] == MINGCHAN_APP_ID


def test_mingchan_bootstrap_rejects_legacy_zhaoxi_world(fresh_db):
    """旧朝夕测试 World 未 cleanup 时必须明确失败，不能串读为鸣蝉 World。"""
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037929")
    # 鸣蝉 persistence 会主动拒绝创建跨产品 World；这里直接模拟 cleanup 前的旧库行。
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO universes(id, owner_platform_user_id, app_id) VALUES (?, ?, ?)",
            ("uni_legacy_zhaoxi_world", user["id"], ZHAOXI_APP_ID),
        )
    login = create_mingchan_login_session(
        phone="13800037929",
        verified_token=_verified_token("13800037929"),
        registry=registry,
    )
    app = FastAPI()
    install_public_routes(app, registry=registry, config=fresh_db)

    response = TestClient(app).post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers={"Authorization": f"Bearer {login['session']['token']}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "legacy_world_cleanup_required"


def test_mingchan_confirm_creates_only_mingchan_resident_account(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, _registry = _client_and_login(fresh_db, "13800037922")
    candidates = client.post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers=headers,
    ).json()["data"]["candidates"]
    body = {
        "selections": [
            {
                "template_id": candidates[0]["template_id"],
                "display_name": "鸣蝉居民",
            }
        ]
    }

    confirmed = client.post(
        "/api/v1/products/mingchan/worlds/home/residents/confirm",
        headers=headers,
        json=body,
    )
    replay = client.post(
        "/api/v1/products/mingchan/worlds/home/residents/confirm",
        headers=headers,
        json=body,
    )
    listing = client.get(
        "/api/v1/products/mingchan/worlds/home/residents",
        headers=headers,
    )

    assert confirmed.status_code == replay.status_code == listing.status_code == 200
    residents = confirmed.json()["data"]["residents"]
    assert len(residents) == 1
    assert residents[0]["name"] == "鸣蝉居民"
    assert replay.json()["data"]["residents"] == residents
    assert listing.json()["data"]["residents"] == residents
    assert "runtime_account_id" not in listing.text
    with db.connect() as conn:
        account = conn.execute(
            """
            SELECT a.id, a.app_id
            FROM universe_residents r
            JOIN accounts a ON a.id = r.runtime_account_id
            WHERE r.status = 'active'
            """
        ).fetchone()
        assert account["app_id"] == MINGCHAN_APP_ID
        bindings = conn.execute(
            "SELECT COUNT(*) AS n FROM account_owner_bindings WHERE account_id = ?",
            (account["id"],),
        ).fetchone()["n"]
        messages = conn.execute(
            "SELECT message_id, content FROM messages WHERE account_id = ?",
            (account["id"],),
        ).fetchall()
        intro_posts = conn.execute(
            """
            SELECT p.text
            FROM universe_posts p
            JOIN universe_residents r ON r.id = p.author_resident_id
            WHERE r.runtime_account_id = ? AND p.source_type = 'resident_intro'
            """,
            (account["id"],),
        ).fetchall()
        intro_outbox = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM companion_world_outbox o
            JOIN universe_posts p ON p.id = o.post_id
            WHERE p.author_resident_id = (
                SELECT id FROM universe_residents WHERE runtime_account_id = ?
            ) AND p.source_type = 'resident_intro'
            """,
            (account["id"],),
        ).fetchone()["n"]
    assert bindings == 0
    expected = RESIDENT_INTRO_CONTENT["linxiaoman"]
    assert [(row["message_id"], row["content"]) for row in messages] == [
        (f"welcome-{residents[0]['resident_id']}", expected.welcome_message)
    ]
    assert [row["text"] for row in intro_posts] == [expected.intro_post]
    assert intro_outbox == 1


def test_mingchan_bootstrap_never_carries_zhaoxi_legacy_account(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037923")
    legacy = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="朝夕旧账号",
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    login = create_mingchan_login_session(
        phone="13800037923",
        verified_token=_verified_token("13800037923"),
        registry=registry,
    )
    app = FastAPI()
    install_public_routes(app, registry=registry, config=fresh_db)
    client = TestClient(app)

    result = client.post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers={"Authorization": f"Bearer {login['session']['token']}"},
    )

    assert result.status_code == 200
    assert result.json()["data"]["existing_residents"] == []
    with db.connect() as conn:
        legacy_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM universe_residents WHERE origin = 'legacy'"
        ).fetchone()["n"]
        assert conn.execute(
            "SELECT app_id FROM accounts WHERE id = ?",
            (legacy["account"]["id"],),
        ).fetchone()["app_id"] == ZHAOXI_APP_ID
    assert legacy_rows == 0


def test_mingchan_world_rejects_wrong_audience_and_disabled_product(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037924")
    zhaoxi_session = db.create_platform_user_session(
        platform_user_id=user["id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    enabled_app = FastAPI()
    install_public_routes(enabled_app, registry=registry, config=fresh_db)

    wrong_audience = TestClient(enabled_app).post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers={"Authorization": f"Bearer {zhaoxi_session['token']}"},
    )
    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["code"] == "unauthorized"

    disabled_app = FastAPI()
    install_public_routes(disabled_app, config=fresh_db)
    disabled = TestClient(disabled_app).post(
        "/api/v1/products/mingchan/worlds/home/bootstrap"
    )
    assert disabled.status_code == 503
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM universes").fetchone()["n"] == 0


def test_mingchan_resident_reads_fail_closed_on_account_product_mismatch(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    _seed_catalog()
    client, headers, registry = _client_and_login(fresh_db, "13800037925")
    bootstrap = client.post(
        "/api/v1/products/mingchan/worlds/home/bootstrap",
        headers=headers,
    ).json()["data"]
    confirmed = client.post(
        "/api/v1/products/mingchan/worlds/home/residents/confirm",
        headers=headers,
        json={
            "selections": [
                {"template_id": bootstrap["candidates"][0]["template_id"]}
            ]
        },
    )
    assert confirmed.status_code == 200
    principal = db.resolve_session_principal(
        token=headers["Authorization"].removeprefix("Bearer "),
        expected_app_id=MINGCHAN_APP_ID,
        registry=registry,
    )
    db.ensure_product_membership(
        platform_user_id=principal.platform_user_id,
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    with db.connect() as conn:
        account_id = conn.execute(
            "SELECT runtime_account_id FROM universe_residents WHERE status = 'active'"
        ).fetchone()["runtime_account_id"]
        conn.execute(
            "UPDATE accounts SET app_id = ? WHERE id = ?",
            (ZHAOXI_APP_ID, account_id),
        )

    response = client.get(
        "/api/v1/products/mingchan/worlds/home/residents",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "resident_product_mismatch"
