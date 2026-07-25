"""M3-4 App 通知 API、保留与 cleanup 功能测试。"""
from datetime import timedelta

import app.db as db
from app.products.zhaoxi.application import AppInboxAdapter, AppInboxIntent
from app.time_utils import beijing_now
from tests.factories import make_resident_account


def _ready_user(phone: str, name: str):
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name=name
    )["id"]
    account_id = make_resident_account(user_id, name)
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_id)
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    session = db.create_platform_user_session(platform_user_id=user_id)
    return (
        user_id,
        account_id,
        scope,
        {"Authorization": f"Bearer {session['token']}"},
    )


def _deliver(account_id: str, key: str, text: str, *, now=None):
    return AppInboxAdapter().deliver(
        AppInboxIntent(
            runtime_account_id=account_id,
            category="companion_followup",
            source_type="commitment",
            source_id=key,
            idempotency_key=f"resident-obligation:v1:commitment:{key}",
            body_text=text,
            target_type="conversation",
            target_id=f"conv-{key}",
        ),
        now=now or beijing_now(),
    )


def test_notification_routes_require_p1_and_inbox_flags(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    response = client.get("/v1/notifications")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.headers["Cache-Control"] == "no-store"

    fresh_db.companion_world_app_inbox_enabled = True
    fresh_db.companion_world_p1_enabled = False
    response = client.get("/v1/notifications")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_visible_notification_list_count_read_and_owner_isolation(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    user_a, account_a, _scope_a, headers_a = _ready_user(
        "19964001001", "通知用户甲"
    )
    _user_b, _account_b, _scope_b, headers_b = _ready_user(
        "19964001002", "通知用户乙"
    )
    item, created = _deliver(account_a, "visible-1", "记得看看今天的小约定")
    replay, replay_created = _deliver(
        account_a, "visible-1", "记得看看今天的小约定"
    )
    assert created is True and replay_created is False and replay.id == item.id

    listed = client.get("/api/v1/notifications", headers=headers_a)
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert data["unread_count"] == 1 and len(data["items"]) == 1
    public = data["items"][0]
    assert public["notification_id"] == item.id
    assert public["resident"]["name"] == "通知用户甲"
    assert public["body"] == {
        "type": "text",
        "text": "记得看看今天的小约定",
    }
    assert public["status"] == "unread"
    assert listed.headers["Cache-Control"] == "no-store"
    assert "runtime_account_id" not in listed.text
    assert "request_fingerprint" not in listed.text

    other = client.get("/v1/notifications", headers=headers_b).json()["data"]
    assert other["items"] == [] and other["unread_count"] == 0
    forbidden = client.post(
        f"/v1/notifications/{item.id}/read", headers=headers_b
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "notification_not_found"
    injected_owner = client.post(
        "/v1/notifications/read-all",
        headers=headers_a,
        json={"account_id": account_a},
    )
    assert injected_owner.status_code == 400
    assert injected_owner.json()["code"] == "account_id_not_accepted"

    first_read = client.post(
        f"/v1/notifications/{item.id}/read", headers=headers_a
    )
    second_read = client.post(
        f"/v1/notifications/{item.id}/read", headers=headers_a
    )
    assert first_read.status_code == second_read.status_code == 200
    first_item = first_read.json()["data"]["notification"]
    second_item = second_read.json()["data"]["notification"]
    assert first_item["read_at"] == second_item["read_at"]
    assert first_item["expires_at"] == second_item["expires_at"]
    assert client.get(
        "/v1/notifications/unread-count", headers=headers_a
    ).json()["data"]["unread_count"] == 0
    assert user_a not in listed.text


def test_notification_cursor_read_all_and_logical_expiry(client, fresh_db):
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_app_inbox_enabled = True
    user_id, account_id, _scope, headers = _ready_user(
        "19964001003", "通知分页用户"
    )
    current = beijing_now().replace(microsecond=0)
    created_ids = set()
    for index in range(3):
        item, _ = _deliver(
            account_id,
            f"page-{index}",
            f"分页通知 {index}",
            now=current + timedelta(seconds=index),
        )
        created_ids.add(item.id)

    seen = []
    cursor = None
    for _ in range(3):
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/notifications", headers=headers, params=params)
        assert page.status_code == 200
        payload = page.json()["data"]
        seen.append(payload["items"][0]["notification_id"])
        cursor = payload["next_cursor"]
    assert set(seen) == created_ids and cursor is None

    read_all = client.post("/v1/notifications/read-all", headers=headers)
    assert read_all.status_code == 200
    assert read_all.json()["data"]["marked_count"] == 3
    unread = client.get(
        "/v1/notifications", headers=headers, params={"status": "unread"}
    ).json()["data"]
    assert unread["items"] == [] and unread["unread_count"] == 0

    with db.connect() as conn:
        expired_id = next(iter(created_ids))
        conn.execute(
            "UPDATE app_notifications SET expires_at='2000-01-01 00:00:00' "
            "WHERE id=? AND platform_user_id=?",
            (expired_id, user_id),
        )
    assert len(
        client.get("/v1/notifications", headers=headers).json()["data"]["items"]
    ) == 2
    invalid = client.get(
        "/v1/notifications", headers=headers, params={"cursor": "bad!"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_cursor"


def test_notification_limit_prefers_read_then_oldest_unread_and_cleanup(fresh_db):
    user_id, account_id, scope, _headers = _ready_user(
        "19964001004", "通知清理用户"
    )
    base = beijing_now().replace(microsecond=0)
    rows = []
    for index in range(3):
        delivered = base + timedelta(seconds=index)
        row, _ = db.insert_visible_app_notification(
            platform_user_id=user_id,
            universe_id=scope["universe_id"],
            resident_id=scope["resident_id"],
            scope="resident",
            category="companion_followup",
            source_type="commitment",
            source_id=f"limit-{index}",
            idempotency_key=f"resident-obligation:v1:commitment:limit-{index}",
            request_fingerprint=f"fingerprint-{index}",
            title=None,
            body_text=f"上限通知 {index}",
            target_type="none",
            target_id=None,
            delivered_at=delivered.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(delivered + timedelta(days=30)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            now=delivered.strftime("%Y-%m-%d %H:%M:%S"),
            max_visible=3,
        )
        rows.append(row)
    db.mark_app_notification_read(
        notification_id=rows[1]["id"],
        platform_user_id=user_id,
        now=(base + timedelta(seconds=4)).strftime("%Y-%m-%d %H:%M:%S"),
        read_expires_at=(base + timedelta(days=7)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )
    fourth, _ = db.insert_visible_app_notification(
        platform_user_id=user_id,
        universe_id=scope["universe_id"],
        resident_id=scope["resident_id"],
        scope="resident",
        category="companion_followup",
        source_type="commitment",
        source_id="limit-3",
        idempotency_key="resident-obligation:v1:commitment:limit-3",
        request_fingerprint="fingerprint-3",
        title=None,
        body_text="上限通知 3",
        target_type="none",
        target_id=None,
        delivered_at=(base + timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
        expires_at=(base + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
        now=(base + timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
        max_visible=3,
    )
    with db.connect() as conn:
        ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM app_notifications WHERE platform_user_id=? "
                "AND delivery_status='visible'",
                (user_id,),
            ).fetchall()
        }
    assert rows[1]["id"] not in ids
    assert rows[0]["id"] in ids and fourth["id"] in ids

    reserved, _ = db.reserve_human_app_notification(
        platform_user_id=user_id,
        universe_id=scope["universe_id"],
        category="content_invitation",
        source_type="account_check",
        source_id="cleanup-reservation",
        idempotency_key="human-proactive:v1:content_invitation:cleanup",
        request_fingerprint="cleanup-reservation",
        claim_token="expired-claim",
        claim_expires_at="2000-01-01 00:00:00",
        now="1999-12-31 23:59:00",
        visible_since="1999-12-30 00:00:00",
    )
    cleanup = db.cleanup_app_notifications_batch(
        now="2026-07-22 12:00:00", limit=100, max_visible=3
    )
    assert cleanup["cancelled_reservations"] == 1
    with db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM app_notifications WHERE id=?", (reserved["id"],)
        ).fetchone()
    assert current["delivery_status"] == "cancelled"
    assert current["terminal_reason"] == "claim_expired"
    assert account_id
