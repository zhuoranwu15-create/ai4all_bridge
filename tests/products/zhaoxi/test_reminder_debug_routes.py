from app.db import cancel_reminder, create_reminder, get_or_create_session

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _setup_account(account_id: str) -> None:
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"sk-{account_id}",
    )


def _create_reminder(account_id: str, reminder_id: str) -> dict:
    return create_reminder(
        reminder_id=reminder_id,
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@wechat",
        session_key=f"sk-{account_id}",
        text="喝水提醒",
        due_at="2026-06-01 10:00:00",
    )


def test_debug_get_reminders_empty(client, fresh_db):
    _setup_account("rdb-empty")
    res = client.get("/debug/reminders/rdb-empty", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["account_id"] == "rdb-empty"
    assert data["reminders"] == []


def test_debug_get_reminders_returns_multiple_statuses(client, fresh_db):
    _setup_account("rdb-list")
    _create_reminder("rdb-list", "rem-p")
    _create_reminder("rdb-list", "rem-c")
    cancel_reminder(reminder_id="rem-c")

    res = client.get("/debug/reminders/rdb-list", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    ids = {r["id"] for r in res.json()["reminders"]}
    assert ids == {"rem-p", "rem-c"}


def test_debug_patch_reminder_updates_fields(client, fresh_db):
    _setup_account("rdb-patch")
    _create_reminder("rdb-patch", "rem-patch")

    res = client.patch(
        "/debug/reminders/rem-patch",
        headers=ADMIN_HEADERS,
        json={"text": "新内容", "due_at": "2026-07-01 08:00:00"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["reminder"]["text"] == "新内容"
    assert data["reminder"]["due_at"] == "2026-07-01 08:00:00"


def test_debug_patch_reminder_rejects_non_pending(client, fresh_db):
    _setup_account("rdb-patch-bad")
    _create_reminder("rdb-patch-bad", "rem-bad")
    cancel_reminder(reminder_id="rem-bad")

    res = client.patch(
        "/debug/reminders/rem-bad",
        headers=ADMIN_HEADERS,
        json={"text": "新内容"},
    )
    assert res.status_code == 400


def test_debug_delete_reminder_cancels_pending(client, fresh_db):
    _setup_account("rdb-del")
    _create_reminder("rdb-del", "rem-del")

    res = client.delete("/debug/reminders/rem-del", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["reminder"]["status"] == "cancelled"


def test_debug_delete_reminder_rejects_non_pending(client, fresh_db):
    _setup_account("rdb-del-bad")
    _create_reminder("rdb-del-bad", "rem-del-bad")
    cancel_reminder(reminder_id="rem-del-bad")

    res = client.delete("/debug/reminders/rem-del-bad", headers=ADMIN_HEADERS)
    assert res.status_code == 400


def test_debug_routes_require_admin_auth(client, fresh_db):
    res = client.get("/debug/reminders/some-account")
    assert res.status_code == 401
    res = client.patch("/debug/reminders/some-id", json={})
    assert res.status_code == 401
    res = client.delete("/debug/reminders/some-id")
    assert res.status_code == 401
