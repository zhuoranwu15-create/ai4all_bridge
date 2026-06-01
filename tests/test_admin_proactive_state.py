from unittest.mock import patch


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def _insert_history(account_id: str, text: str) -> None:
    from app.db import insert_message, list_sessions_for_account

    sessions = list_sessions_for_account(account_id=account_id)
    assert sessions
    insert_message(
        account_id=account_id,
        session_id=int(sessions[0]["id"]),
        message_id=f"history-{account_id}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=text,
        raw={"source": "test"},
    )


def test_admin_proactive_state_create_get_and_scan_no_op(client, fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.state import get_account_state

    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    _create_account("acc-admin-proactive")

    before = client.get(
        "/admin/accounts/acc-admin-proactive/proactive-state",
        headers=ADMIN_HEADERS,
    )
    assert before.status_code == 200
    assert before.json()["proactive_state"] is None

    updated = client.patch(
        "/admin/accounts/acc-admin-proactive/proactive-state",
        json={
            "enabled": True,
            "next_scan_at": "2000-01-01T00:00:00",
            "metadata": {"source": "admin-test"},
        },
        headers=ADMIN_HEADERS,
    )
    assert updated.status_code == 200
    state = updated.json()["proactive_state"]
    assert state["enabled"] is True
    assert state["next_scan_at"] == "2000-01-01 00:00:00"
    assert state["metadata"]["source"] == "admin-test"

    fetched = client.get(
        "/admin/accounts/acc-admin-proactive/proactive-state",
        headers=ADMIN_HEADERS,
    )
    assert fetched.status_code == 200
    assert fetched.json()["proactive_state"]["next_scan_at"] == "2000-01-01 00:00:00"

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.proactive.messaging.send_weixin_text") as mock_send,
    ):
        run = client.post(
            "/admin/proactive/scheduler/run-once?limit=5",
            headers=ADMIN_HEADERS,
        )

    assert run.status_code == 200
    body = run.json()
    assert body["run"]["account_check_count"] == 1
    assert body["run"]["account_checks"][0]["account_id"] == "acc-admin-proactive"
    assert body["run"]["account_checks"][0]["status"] == "skipped"
    assert body["run"]["account_checks"][0]["reason"] == "no_candidate"
    mock_send.assert_not_called()
    assert list_outbound_messages(account_id="acc-admin-proactive") == []

    scanned_state = get_account_state(account_id="acc-admin-proactive")
    assert scanned_state["last_scan_at"] is not None
    assert scanned_state["next_scan_at"] > scanned_state["last_scan_at"]


def test_admin_proactive_state_disabled_account_is_not_scanned(client):
    _create_account("acc-admin-proactive-disabled")

    updated = client.patch(
        "/admin/accounts/acc-admin-proactive-disabled/proactive-state",
        json={
            "enabled": False,
            "next_scan_at": "2000-01-01 00:00:00",
        },
        headers=ADMIN_HEADERS,
    )
    assert updated.status_code == 200

    run = client.post(
        "/admin/proactive/scheduler/run-once?limit=5",
        headers=ADMIN_HEADERS,
    )

    assert run.status_code == 200
    assert run.json()["run"]["account_check_count"] == 0


def test_admin_proactive_state_validates_datetime(client):
    _create_account("acc-admin-proactive-invalid")

    res = client.patch(
        "/admin/accounts/acc-admin-proactive-invalid/proactive-state",
        json={"next_scan_at": "not-a-date"},
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 400
    assert "next_scan_at" in res.json()["detail"]


def test_admin_proactive_state_requires_account(client):
    res = client.get(
        "/admin/accounts/missing/proactive-state",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 404

    res = client.patch(
        "/admin/accounts/missing/proactive-state",
        json={"enabled": True},
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 404

    res = client.post(
        "/admin/accounts/missing/proactive-check-candidate-draft",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 404


def test_admin_generates_account_check_candidate_draft(client, fresh_db):
    from app.proactive.state import get_account_state

    fresh_db.llm_api_key = "fake-key"
    _create_account("acc-admin-check-draft")
    _create_route("acc-admin-check-draft")
    _insert_history("acc-admin-check-draft", "我昨天说今天要检查事情 A，后续 B 明天也要看。")
    client.patch(
        "/admin/accounts/acc-admin-check-draft/proactive-state",
        json={"enabled": True, "next_scan_at": "2000-01-01 00:00:00"},
        headers=ADMIN_HEADERS,
    )

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch(
            "app.proactive.account_checks.generate_completion",
            return_value=(
                '{"should_send": true, "text": "记得看一下后续 B。", '
                '"reason": "用户明确提到后续 B", "confidence": 0.91}'
            ),
        ),
    ):
        res = client.post(
            "/admin/accounts/acc-admin-check-draft/proactive-check-candidate-draft",
            headers=ADMIN_HEADERS,
        )
        state = get_account_state(account_id="acc-admin-check-draft")

    assert res.status_code == 200
    body = res.json()
    assert body["result"]["action"] == "draft_candidate"
    assert body["result"]["candidate"]["text"] == "记得看一下后续 B。"
    assert state["metadata"]["account_check_candidate_draft"]["text"] == "记得看一下后续 B。"
    assert "account_check_candidate" not in state["metadata"]


def test_admin_promotes_and_clears_account_check_candidate_draft(client):
    from app.proactive.state import get_account_state

    _create_account("acc-admin-check-promote")
    _create_route("acc-admin-check-promote")
    client.patch(
        "/admin/accounts/acc-admin-check-promote/proactive-state",
        json={
            "enabled": True,
            "next_scan_at": "2000-01-01 00:00:00",
            "metadata": {
                "account_check_candidate_draft": {
                    "id": "draft-admin",
                    "text": "这条由 draft 提升。",
                    "source": "test",
                }
            },
        },
        headers=ADMIN_HEADERS,
    )

    promote = client.post(
        "/admin/accounts/acc-admin-check-promote/proactive-check-candidate-draft/promote",
        headers=ADMIN_HEADERS,
    )
    promoted_state = get_account_state(account_id="acc-admin-check-promote")

    assert promote.status_code == 200
    assert promote.json()["result"]["action"] == "promoted_candidate"
    assert promoted_state["metadata"]["account_check_candidate"]["text"] == "这条由 draft 提升。"
    assert "account_check_candidate_draft" not in promoted_state["metadata"]

    client.patch(
        "/admin/accounts/acc-admin-check-promote/proactive-state",
        json={
            "metadata": {
                **promoted_state["metadata"],
                "account_check_candidate_draft": {"text": "清理这个 draft"},
            }
        },
        headers=ADMIN_HEADERS,
    )
    clear = client.delete(
        "/admin/accounts/acc-admin-check-promote/proactive-check-candidate-draft",
        headers=ADMIN_HEADERS,
    )
    cleared_state = get_account_state(account_id="acc-admin-check-promote")

    assert clear.status_code == 200
    assert clear.json()["result"]["action"] == "cleared_candidate_draft"
    assert "account_check_candidate_draft" not in cleared_state["metadata"]
    assert cleared_state["metadata"]["account_check_candidate"]["text"] == "这条由 draft 提升。"


def test_admin_lists_and_cancels_commitments(client):
    from app.db import create_proactive_commitment, get_proactive_commitment

    _create_account("acc-admin-commitments")
    create_proactive_commitment(
        commitment_id="com-admin",
        account_id="acc-admin-commitments",
        text="看一下后续 B。",
        due_at="2026-05-23 09:30:00",
        confidence=0.95,
        reason="admin test",
    )

    listed = client.get(
        "/admin/accounts/acc-admin-commitments/commitments",
        headers=ADMIN_HEADERS,
    )
    cancelled = client.post(
        "/admin/commitments/com-admin/cancel",
        headers=ADMIN_HEADERS,
    )
    commitment = get_proactive_commitment(commitment_id="com-admin")

    assert listed.status_code == 200
    assert listed.json()["commitments"][0]["id"] == "com-admin"
    assert cancelled.status_code == 200
    assert cancelled.json()["commitment"]["status"] == "cancelled"
    assert commitment["status"] == "cancelled"
    assert commitment["error"] == "admin_cancelled"


def test_admin_proactive_overview_redacts_task_text(client):
    from app.db import (
        create_outbound_message,
        create_proactive_commitment,
        create_reminder,
        upsert_proactive_account_state,
    )

    secret_reminder = "提醒我去医院取报告"
    secret_commitment = "明天跟进用户提到的后续 B"
    secret_outbound = "主动消息正文"

    _create_account("acc-admin-overview")
    upsert_proactive_account_state(
        account_id="acc-admin-overview",
        enabled=True,
        next_scan_at="2026-05-31 09:00:00",
        metadata={
            "account_check_candidate": {
                "id": "candidate-1",
                "text": "候选正文也要脱敏",
            }
        },
    )
    create_reminder(
        reminder_id="rem-admin-overview",
        account_id="acc-admin-overview",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-admin-overview",
        text=secret_reminder,
        due_at="2026-06-01 10:00:00",
        metadata={"text": "metadata 里的正文也要脱敏"},
    )
    create_proactive_commitment(
        commitment_id="com-admin-overview",
        account_id="acc-admin-overview",
        text=secret_commitment,
        due_at="2026-06-01 11:00:00",
        confidence=0.95,
        reason="admin overview",
    )
    create_outbound_message(
        account_id="acc-admin-overview",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-admin-overview",
        source="reminder",
        text=secret_outbound,
        idempotency_key="admin-overview-outbound",
        quota_date="2026-05-31",
        status="failed",
        error="gateway timeout",
        metadata={"message": "metadata outbound text"},
    )

    res = client.get(
        "/admin/accounts/acc-admin-overview/proactive-overview",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["redacted"] is True
    assert body["proactive_state"]["metadata"]["account_check_candidate"]["text"]["redacted"] is True
    assert body["reminders"][0]["text_redacted"] is True
    assert body["reminders"][0]["text_chars"] == len(secret_reminder)
    assert body["reminders"][0]["metadata"]["text"]["redacted"] is True
    assert body["commitments"][0]["text_redacted"] is True
    assert body["commitments"][0]["text_chars"] == len(secret_commitment)
    assert body["commitments"][0]["reason_redacted"] is True
    assert body["outbound_messages"][0]["text_redacted"] is True
    assert body["outbound_messages"][0]["text_chars"] == len(secret_outbound)
    dumped = str(body)
    assert secret_reminder not in dumped
    assert secret_commitment not in dumped
    assert secret_outbound not in dumped
