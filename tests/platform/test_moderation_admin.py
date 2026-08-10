from pathlib import Path
from typing import Optional


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
REVIEWER_HEADERS = {"Authorization": "Bearer test-reviewer"}


def _ensure_account(account_id: str = "acc-mod-admin") -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _task(
    *,
    app_id: str = "zhaoxi",
    account_id: str = "acc-mod-admin",
    text: str = "moderation detail text",
    status: str = "needs_review",
    risk_level: str = "review",
    risk_categories: Optional[list[str]] = None,
):
    from app.db import create_content_moderation_task

    if app_id == "zhaoxi":
        _ensure_account(account_id)
    else:
        from app.db import connect

        with connect() as conn:
            conn.execute(
                "INSERT INTO accounts(id, channel, app_id) VALUES (?, 'native', ?)",
                (account_id, app_id),
            )
            conn.execute(
                "INSERT INTO profiles(account_id) VALUES (?)",
                (account_id,),
            )
    suffix = abs(hash((account_id, text, status, risk_level)))
    return create_content_moderation_task(
        app_id=app_id,
        account_id=account_id,
        session_id=None,
        source_type="message",
        source_id=f"source-{suffix}",
        message_db_id=None,
        outbound_message_id=None,
        direction="inbound",
        content_kind="text",
        status=status,
        risk_level=risk_level,
        risk_categories=risk_categories or ["test_review"],
        snapshot_text=text,
        media={},
        sampling_reason="test",
        sample_rate_percent=100,
        policy_version="test_policy",
        idempotency_key=f"admin:{account_id}:{suffix}",
        metadata={"test": True},
    )


def test_reviewer_queue_detail_claim_and_decision_are_audited(client):
    from app.db import get_content_moderation_task

    task = _task(text="reviewer can see this only in detail")

    res = client.get("/admin/moderation/tasks?status=needs_review", headers=REVIEWER_HEADERS)
    assert res.status_code == 200
    listed = res.json()["tasks"][0]
    assert listed["id"] == task["id"]
    assert "snapshot_text" not in listed
    assert listed["snapshot_text_chars"] == len("reviewer can see this only in detail")

    res = client.get(f"/admin/moderation/tasks/{task['id']}?reason=queue-review", headers=REVIEWER_HEADERS)
    assert res.status_code == 200
    detail = res.json()
    assert detail["plaintext"] is True
    assert detail["task"]["snapshot_text"] == "reviewer can see this only in detail"

    res = client.post(f"/admin/moderation/tasks/{task['id']}/claim", headers=REVIEWER_HEADERS)
    assert res.status_code == 200
    assert res.json()["task"]["status"] == "reviewing"
    assert res.json()["task"]["assigned_admin_user_id"] == "reviewer"

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/decision",
        headers=REVIEWER_HEADERS,
        json={"decision": "approved", "reason": "人工通过"},
    )
    assert res.status_code == 200
    updated = get_content_moderation_task(task_id=task["id"])
    assert updated["status"] == "approved"
    assert updated["risk_level"] == "pass"
    assert updated["reviewed_by_admin_user_id"] == "reviewer"

    res = client.get("/admin/access-events?plaintext=true", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    actions = [event["action"] for event in res.json()["events"]]
    assert "moderation.view_task" in actions
    assert "moderation.approved" in actions


def test_admin_can_filter_moderation_queue_and_stats_by_app(client):
    zhaoxi_task = _task(account_id="acc-mod-zhaoxi", text="zhaoxi queue")
    mingchan_task = _task(
        app_id="mingchan",
        account_id="acc-mod-mingchan",
        text="mingchan queue",
    )

    res = client.get(
        "/admin/moderation/tasks?app_id=mingchan",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    tasks = res.json()["tasks"]
    assert [task["id"] for task in tasks] == [mingchan_task["id"]]
    assert tasks[0]["app_id"] == "mingchan"
    assert zhaoxi_task["id"] not in {task["id"] for task in tasks}

    res = client.get(
        "/admin/moderation/stats?app_id=mingchan",
        headers=ADMIN_HEADERS,
    )
    assert res.status_code == 200
    assert res.json()["stats"]["total"] == 1
    assert res.json()["stats"]["review_queue_count"] == 1


def test_reviewer_cannot_use_non_moderation_or_admin_only_paths(client):
    task = _task(text="reviewer restricted paths")

    res = client.get("/admin/accounts", headers=REVIEWER_HEADERS)
    assert res.status_code == 403

    res = client.post(
        "/admin/plaintext-grants",
        headers=REVIEWER_HEADERS,
        json={
            "reason": "should not be allowed",
            "account_scope": ["acc-mod-admin"],
            "resource_scope": ["message"],
        },
    )
    assert res.status_code == 403

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/export",
        headers=REVIEWER_HEADERS,
        json={"reason": "reviewer export"},
    )
    assert res.status_code == 403

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/actions",
        headers=REVIEWER_HEADERS,
        json={"action": "disable_account", "reason": "not allowed"},
    )
    assert res.status_code == 403

    client.post(f"/admin/moderation/tasks/{task['id']}/claim", headers=REVIEWER_HEADERS)
    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/decision",
        headers=REVIEWER_HEADERS,
        json={
            "decision": "risk_confirmed",
            "risk_categories": ["test_review"],
            "actions": ["restrict_proactive"],
            "reason": "reviewer cannot restrict",
        },
    )
    assert res.status_code == 403


def test_reviewer_cannot_view_public_machine_passed_task(client):
    task = _task(
        text="machine passed private text",
        status="machine_passed",
        risk_level="pass",
        risk_categories=[],
    )

    res = client.get("/admin/moderation/tasks", headers=REVIEWER_HEADERS)
    assert res.status_code == 200
    assert task["id"] not in {item["id"] for item in res.json()["tasks"]}

    res = client.get(f"/admin/moderation/tasks/{task['id']}", headers=REVIEWER_HEADERS)
    assert res.status_code == 403

    res = client.get(f"/admin/moderation/tasks/{task['id']}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert res.json()["task"]["snapshot_text"] == "machine passed private text"


def test_admin_can_export_single_moderation_task_and_view_artifact(client):
    task = _task(text="exported moderation text", status="reviewing")

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/export",
        headers=ADMIN_HEADERS,
        json={"reason": "合规留存"},
    )
    assert res.status_code == 200
    export = res.json()["export"]
    assert export["task_id"] == task["id"]
    assert Path(export["artifact_path"]).exists()

    res = client.get(f"/admin/moderation/exports/{export['id']}", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["plaintext"] is True
    assert body["artifact"]["task"]["id"] == task["id"]
    assert body["artifact"]["task"]["account_id"] == task["account_id"]
    assert body["artifact"]["task"]["snapshot_text"] == "exported moderation text"

    res = client.get("/admin/access-events?plaintext=true", headers=ADMIN_HEADERS)
    actions = [event["action"] for event in res.json()["events"]]
    assert "moderation.export" in actions
    assert "moderation.view_export" in actions


def test_admin_actions_restrict_proactive_and_disable_account(client):
    from app.db import get_account, get_moderation_account_risk_state
    from app.products.zhaoxi.proactive.preferences import get_effective_proactive_message_settings

    task = _task(account_id="acc-mod-action", text="confirmed risk")
    until = "2099-01-01 00:00:00"

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/actions",
        headers=ADMIN_HEADERS,
        json={
            "action": "restrict_proactive",
            "proactive_blocked_until": until,
            "reason": "人工确认风险后暂停主动消息",
        },
    )
    assert res.status_code == 200
    risk_state = get_moderation_account_risk_state(account_id="acc-mod-action")
    assert risk_state["risk_level"] == "restricted"
    assert risk_state["proactive_blocked_until"] == until
    assert get_effective_proactive_message_settings("acc-mod-action")["muted_until"] == until

    res = client.post(
        f"/admin/moderation/tasks/{task['id']}/actions",
        headers=ADMIN_HEADERS,
        json={"action": "disable_account", "reason": "严重风险"},
    )
    assert res.status_code == 200
    assert get_account(account_id="acc-mod-action")["status"] == "disabled"
    risk_state = get_moderation_account_risk_state(account_id="acc-mod-action")
    assert risk_state["risk_level"] == "disabled"
def test_restrict_proactive_rejects_non_zhaoxi_product_without_side_effects():
    from fastapi import HTTPException

    from app.platform.moderation.admin import _apply_moderation_admin_action

    try:
        _apply_moderation_admin_action(
            task={"id": "task-mingchan", "app_id": "mingchan", "account_id": "acc-mingchan", "status": "needs_review"},
            action="restrict_proactive",
            admin_user={"id": "admin-1", "role": "admin"},
            reason="scope test",
            request_path="/admin/moderation/tasks/task-mingchan/actions",
        )
    except HTTPException as err:
        assert err.status_code == 400
        assert "only supported for the zhaoxi product" in str(err.detail)
    else:
        raise AssertionError("non-zhaoxi restrict_proactive must be rejected")
