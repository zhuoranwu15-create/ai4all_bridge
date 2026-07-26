"""Nooki API 端到端：登录后 chat/profile/state/按钮态接口，以及 session 401 边界。"""
from __future__ import annotations

from app.products.nooki.api import auth as auth_module
from app.products.nooki.domain.goal_breakdown.contracts import PlanDraft
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository

_PREFIX = "/api/v1/products/nooki"


def _bind(client, monkeypatch, *, openid: str, phone: str) -> dict:
    monkeypatch.setattr(
        auth_module,
        "code2session",
        lambda code: {"openid": openid, "session_key": "sess-key", "unionid": None},
    )
    monkeypatch.setattr(
        auth_module,
        "decrypt_phone_number",
        lambda *, encrypted_data, iv, session_key: {
            "phone_number": phone,
            "country_code": "86",
        },
    )
    resp = client.post(
        f"{_PREFIX}/auth/bind",
        json={"code": "js-code", "encrypted_data": "enc", "iv": "iv"},
    )
    assert resp.status_code == 200
    return resp.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_chat_state_profile_and_button_endpoints_require_session(client):
    assert (
        client.post(
            f"{_PREFIX}/chat", json={"text": "你好", "client_message_id": "abcdefgh"}
        ).status_code
        == 401
    )
    assert client.get(f"{_PREFIX}/state").status_code == 401
    assert client.get(f"{_PREFIX}/profile/companion").status_code == 401
    assert (
        client.post(f"{_PREFIX}/tasks/some-task/plans/some-plan/select").status_code
        == 401
    )
    assert client.post(f"{_PREFIX}/steps/some-step/start").status_code == 401
    assert client.post(f"{_PREFIX}/steps/some-step/complete").status_code == 401


def test_companion_profile_defaults_and_roundtrip(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-1", phone="13900011101")
    headers = _auth_headers(bound["access_token"])

    default = client.get(f"{_PREFIX}/profile/companion", headers=headers)
    assert default.status_code == 200
    assert default.json()["status"] == "ok"

    updated = client.post(
        f"{_PREFIX}/profile/companion",
        json={"archetype": "bestie", "companion_name": "阿福"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "status": "ok",
        "archetype": "bestie",
        "companion_name": "阿福",
    }

    fetched = client.get(f"{_PREFIX}/profile/companion", headers=headers)
    assert fetched.json() == {
        "status": "ok",
        "archetype": "bestie",
        "companion_name": "阿福",
    }

    rejected = client.post(
        f"{_PREFIX}/profile/companion",
        json={"archetype": "not_a_real_archetype"},
        headers=headers,
    )
    assert rejected.status_code == 422


def test_state_reflects_no_focus_task_initially(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-2", phone="13900011102")
    headers = _auth_headers(bound["access_token"])

    resp = client.get(f"{_PREFIX}/state", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "has_focus_task": False,
        "active_tasks_count": 0,
    }


def test_chat_returns_reply_and_dedupes_by_client_message_id(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-3", phone="13900011103")
    headers = _auth_headers(bound["access_token"])

    first = client.post(
        f"{_PREFIX}/chat",
        json={"text": "我今天想写周报", "client_message_id": "client-msg-1"},
        headers=headers,
    )
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "ok"
    assert body["reply"] == "mock reply"
    assert body["metadata"]["deduplicated"] is False

    second = client.post(
        f"{_PREFIX}/chat",
        json={"text": "我今天想写周报", "client_message_id": "client-msg-1"},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["metadata"]["deduplicated"] is True


def test_button_endpoints_drive_state_machine_and_reject_other_user(client, monkeypatch):
    owner = _bind(client, monkeypatch, openid="openid-app-4", phone="13900011104")
    owner_headers = _auth_headers(owner["access_token"])
    intruder = _bind(client, monkeypatch, openid="openid-app-5", phone="13900011105")
    intruder_headers = _auth_headers(intruder["access_token"])

    svc = GoalBreakdownService(SqlTaskRepository())
    task = svc.create_task_draft(
        platform_user_id=owner["platform_user_id"],
        title="写周报",
        raw_goal="周报还没写",
        source_message_id=None,
    )
    task = svc.create_step_options(
        task.id,
        (
            PlanDraft(mode="tiny", title="打开文档"),
            PlanDraft(mode="light", title="列提纲"),
            PlanDraft(mode="normal", title="写完"),
        ),
        platform_user_id=owner["platform_user_id"],
    )
    plan = svc._repository.list_plans(task.id)[0]

    forbidden_select = client.post(
        f"{_PREFIX}/tasks/{task.id}/plans/{plan.id}/select", headers=intruder_headers
    )
    assert forbidden_select.status_code == 404

    selected = client.post(
        f"{_PREFIX}/tasks/{task.id}/plans/{plan.id}/select", headers=owner_headers
    )
    assert selected.status_code == 200
    step_id = selected.json()["current_step_id"]
    assert step_id

    forbidden_start = client.post(
        f"{_PREFIX}/steps/{step_id}/start", headers=intruder_headers
    )
    assert forbidden_start.status_code == 404

    started = client.post(f"{_PREFIX}/steps/{step_id}/start", headers=owner_headers)
    assert started.status_code == 200
    assert started.json()["status"] == "active"

    forbidden_complete = client.post(
        f"{_PREFIX}/steps/{step_id}/complete", headers=intruder_headers
    )
    assert forbidden_complete.status_code == 404

    completed = client.post(
        f"{_PREFIX}/steps/{step_id}/complete", headers=owner_headers
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
