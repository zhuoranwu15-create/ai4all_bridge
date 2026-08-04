"""鸣蝉 App 通知 API 的 audience、owner 与产品隔离。"""
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
    verified = db.set_verification_verified(row["id"], token_expires_minutes=10)
    return str(verified["verified_token"])


def _scope(config, phone: str) -> tuple[TestClient, dict, dict]:
    registry = build_test_product_registry()
    for rank in range(1, 5):
        db.create_character_template(
            app_id=MINGCHAN_APP_ID,
            template_id=f"tmpl_mingchan_notification_{rank}",
            source_type="operations",
            name=f"鸣蝉通知居民{rank}",
            avatar_ref=f"asset://mingchan-notification-{rank}",
            summary=f"鸣蝉通知简介{rank}",
            tags_json=json.dumps(["鸣蝉", "通知", str(rank)], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n鸣蝉通知人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 名字：鸣蝉通知居民{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
            persona_key="linxiaoman" if rank == 1 else None,
        )
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
    return TestClient(app), headers, {
        "user_id": user_id,
        "resident": resident,
    }


def _insert_notification(scope: dict, *, app_id: str, key: str) -> dict:
    resident = scope["resident"]
    row, _created = db.insert_visible_app_notification(
        platform_user_id=scope["user_id"],
        app_id=app_id,
        universe_id=resident.universe_id,
        resident_id=resident.resident_id,
        scope="resident",
        category="companion_followup",
        source_type="commitment",
        source_id=key,
        idempotency_key=f"notification:{key}",
        request_fingerprint=f"fingerprint:{key}",
        title=None,
        body_text=f"鸣蝉通知 {key}",
        target_type="conversation",
        target_id=resident.conversation_id,
        delivered_at="2026-08-04 12:00:00",
        expires_at="2099-08-04 12:00:00",
        now="2026-08-04 12:00:00",
    )
    return row


def test_mingchan_notifications_list_read_and_preferences_are_product_scoped(
    fresh_db,
):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    client, headers, scope = _scope(fresh_db, "13800037941")
    item = _insert_notification(scope, app_id=MINGCHAN_APP_ID, key="visible")

    listed = client.get(
        "/api/v1/products/mingchan/notifications",
        headers=headers,
    )
    read = client.post(
        f"/v1/products/mingchan/notifications/{item['id']}/read",
        headers=headers,
    )
    default_preference = client.get(
        "/api/v1/products/mingchan/notifications/preferences",
        headers=headers,
    )
    updated_preference = client.patch(
        "/api/v1/products/mingchan/notifications/preferences",
        headers=headers,
        json={"quiet_level": "quiet"},
    )

    assert listed.status_code == read.status_code == 200
    assert listed.json()["data"]["unread_count"] == 1
    assert listed.json()["data"]["items"][0]["notification_id"] == item["id"]
    assert read.json()["data"]["notification"]["status"] == "read"
    assert default_preference.json()["data"]["quiet_level"] == "standard"
    assert updated_preference.json()["data"]["quiet_level"] == "quiet"
    with db.connect() as conn:
        preference = conn.execute(
            "SELECT app_id, quiet_level FROM product_notification_preferences "
            "WHERE platform_user_id = ?",
            (scope["user_id"],),
        ).fetchone()
    assert dict(preference) == {"app_id": MINGCHAN_APP_ID, "quiet_level": "quiet"}


def test_mingchan_notifications_hide_zhaoxi_rows_and_reject_zhaoxi_token(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    client, headers, scope = _scope(fresh_db, "13800037942")
    visible = _insert_notification(scope, app_id=MINGCHAN_APP_ID, key="mingchan")
    resident = scope["resident"]
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO app_notifications(
                id, platform_user_id, app_id, universe_id, resident_id,
                scope, category, source_type, idempotency_key,
                request_fingerprint, delivery_status, body_text,
                target_type, delivered_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'resident', 'companion_followup',
                      'commitment', ?, ?, 'visible', ?, 'none', ?, ?)
            """,
            (
                "notif_zhaoxi_hidden",
                scope["user_id"],
                ZHAOXI_APP_ID,
                resident.universe_id,
                resident.resident_id,
                "notification:zhaoxi",
                "fingerprint:zhaoxi",
                "朝夕旧通知",
                "2026-08-04 11:00:00",
                "2099-08-04 11:00:00",
            ),
        )
    registry = build_test_product_registry()
    db.ensure_product_membership(
        platform_user_id=scope["user_id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )
    zhaoxi_session = db.create_platform_user_session(
        platform_user_id=scope["user_id"],
        app_id=ZHAOXI_APP_ID,
        registry=registry,
    )

    listed = client.get(
        "/api/v1/products/mingchan/notifications",
        headers=headers,
    )
    wrong_audience = client.get(
        "/api/v1/products/mingchan/notifications",
        headers={"Authorization": f"Bearer {zhaoxi_session['token']}"},
    )

    assert [
        row["notification_id"] for row in listed.json()["data"]["items"]
    ] == [visible["id"]]
    assert wrong_audience.status_code == 401


def test_mingchan_notifications_fail_closed_when_feature_disabled(fresh_db):
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_app_inbox_enabled = True
    client, headers, _scope_data = _scope(fresh_db, "13800037943")
    fresh_db.mingchan_app_inbox_enabled = False

    response = client.get(
        "/api/v1/products/mingchan/notifications",
        headers=headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "feature_disabled"
