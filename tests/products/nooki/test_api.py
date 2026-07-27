"""Nooki App API：统一 state/cards、按钮幂等和账号隔离。"""
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
        lambda login_code: {"openid": openid, "unionid": None},
    )
    monkeypatch.setattr(
        auth_module,
        "get_phone_number",
        lambda phone_code: {"phone_number": phone, "country_code": "86"},
    )
    response = client.post(
        f"{_PREFIX}/auth/bind",
        json={"login_code": "js-code", "phone_code": "phone-code"},
    )
    assert response.status_code == 200
    return response.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _action(request_id: str, version=None) -> dict:
    body = {"client_request_id": request_id}
    if version is not None:
        body["expected_version"] = version
    return body


def _create_task(user_id: str):
    return GoalBreakdownService(SqlTaskRepository()).create_task_with_options(
        platform_user_id=user_id,
        title="写周报",
        raw_goal="我想开始写周报",
        options=(
            PlanDraft("tiny", "打开文档", "看见页面即可", 2),
            PlanDraft("light", "列出提纲", "列三个点", 6),
            PlanDraft("normal", "写完初稿", "暂时不润色", 20),
        ),
        source_message_id=f"seed:{user_id}",
        operation_id=f"seed:create:{user_id}",
    )


def _bootstrap(client, token: str) -> dict:
    response = client.get(f"{_PREFIX}/bootstrap", headers=_headers(token))
    assert response.status_code == 200
    return response.json()


def test_product_endpoints_require_session(client):
    assert client.post(
        f"{_PREFIX}/chat", json={"text": "你好", "client_message_id": "abcdefgh"}
    ).status_code == 401
    assert client.get(f"{_PREFIX}/state").status_code == 401
    assert client.get(f"{_PREFIX}/bootstrap").status_code == 401
    assert client.get(f"{_PREFIX}/profile/companion").status_code == 401
    assert client.post(
        f"{_PREFIX}/tasks/task/plans/plan/select", json=_action("request01")
    ).status_code == 401


def test_companion_profile_defaults_and_roundtrip(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-1", phone="13900011101")
    headers = _headers(bound["access_token"])
    initial = client.get(f"{_PREFIX}/profile/companion", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["configured"] is False
    updated = client.post(
        f"{_PREFIX}/profile/companion",
        json={"archetype": "bestie", "companion_name": "阿福"},
        headers=headers,
    )
    assert updated.json()["companion_name"] == "阿福"
    assert updated.json()["configured"] is True
    assert client.get(f"{_PREFIX}/profile/companion", headers=headers).json()[
        "archetype"
    ] == "bestie"
    assert _bootstrap(client, bound["access_token"])["profile"]["configured"] is True


def test_state_and_chat_share_authoritative_view(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-2", phone="13900011102")
    headers = _headers(bound["access_token"])
    bootstrap = _bootstrap(client, bound["access_token"])
    conversation_id = bootstrap["conversation"]["conversation_id"]
    assert bootstrap["messages"] == []
    assert bootstrap["server_cursor"] is None
    empty = client.get(f"{_PREFIX}/state", headers=headers).json()
    assert empty["state"]["has_focus_task"] is False
    assert empty["cards"] == []

    first = client.post(
        f"{_PREFIX}/chat",
        json={
            "conversation_id": conversation_id,
            "text": "今天有点累",
            "client_message_id": "client-msg-1",
        },
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["reply"] == "mock reply"
    assert first.json()["state"]["has_focus_task"] is False
    assert first.json()["later_items"] == []
    assert first.json()["metadata"]["deduplicated"] is False
    assert first.json()["user_message"]["content"] == "今天有点累"
    assert first.json()["assistant_message"]["content"] == "mock reply"
    assert first.json()["server_cursor"] is not None

    duplicate = client.post(
        f"{_PREFIX}/chat",
        json={
            "conversation_id": conversation_id,
            "text": "今天有点累",
            "client_message_id": "client-msg-1",
        },
        headers=headers,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["metadata"]["deduplicated"] is True
    assert "state" in duplicate.json() and "cards" in duplicate.json()
    assert duplicate.json()["later_items"] == []
    assert duplicate.json()["assistant_message"] == first.json()["assistant_message"]

    history = client.get(
        f"{_PREFIX}/conversations/{conversation_id}/messages?limit=1",
        headers=headers,
    ).json()
    assert [item["role"] for item in history["messages"]] == ["assistant"]
    assert history["has_more"] is True
    older = client.get(
        f"{_PREFIX}/conversations/{conversation_id}/messages",
        params={"before_cursor": history["next_cursor"], "limit": 10},
        headers=headers,
    ).json()
    assert [item["role"] for item in older["messages"]] == ["user"]

    synced = client.get(
        f"{_PREFIX}/sync",
        params={"conversation_id": conversation_id, "after_cursor": "0"},
        headers=headers,
    ).json()
    assert [item["role"] for item in synced["messages"]] == ["user", "assistant"]
    assert synced["conversation_id"] == conversation_id


def test_conversation_is_stable_and_isolated(client, monkeypatch):
    owner = _bind(client, monkeypatch, openid="openid-conv-1", phone="13900011111")
    intruder = _bind(client, monkeypatch, openid="openid-conv-2", phone="13900011112")
    first = _bootstrap(client, owner["access_token"])
    second = _bootstrap(client, owner["access_token"])
    conversation_id = first["conversation"]["conversation_id"]
    assert second["conversation"]["conversation_id"] == conversation_id

    forbidden_history = client.get(
        f"{_PREFIX}/conversations/{conversation_id}/messages",
        headers=_headers(intruder["access_token"]),
    )
    assert forbidden_history.status_code == 404
    forbidden_chat = client.post(
        f"{_PREFIX}/chat",
        json={
            "conversation_id": conversation_id,
            "text": "越权消息",
            "client_message_id": "cross-user-1",
        },
        headers=_headers(intruder["access_token"]),
    )
    assert forbidden_chat.status_code == 404


def test_later_items_are_authoritative_idempotent_and_isolated(client, monkeypatch):
    owner = _bind(client, monkeypatch, openid="openid-later-1", phone="13900011121")
    intruder = _bind(client, monkeypatch, openid="openid-later-2", phone="13900011122")
    headers = _headers(owner["access_token"])

    created = client.post(
        f"{_PREFIX}/later-items",
        json={"content": "整理书桌", "client_request_id": "later-create-1"},
        headers=headers,
    )
    assert created.status_code == 200
    item = created.json()["item"]
    assert item["content"] == "整理书桌"
    assert item["status"] == "inbox"
    assert item["version"] == 1
    repeated = client.post(
        f"{_PREFIX}/later-items",
        json={"content": "不会覆盖", "client_request_id": "later-create-1"},
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["item"] == item
    assert repeated.json()["metadata"]["deduplicated"] is True

    assert _bootstrap(client, owner["access_token"])["later_items"] == [item]
    assert client.get(f"{_PREFIX}/later-items", headers=headers).json()[
        "items"
    ] == [item]
    assert client.get(
        f"{_PREFIX}/later-items", headers=_headers(intruder["access_token"])
    ).json()["items"] == []

    stale = client.patch(
        f"{_PREFIX}/later-items/{item['later_item_id']}",
        json={"content": "擦干净书桌", "expected_version": 99},
        headers=headers,
    )
    assert stale.status_code == 409
    updated = client.patch(
        f"{_PREFIX}/later-items/{item['later_item_id']}",
        json={"content": "擦干净书桌", "expected_version": 1},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["item"]["version"] == 2

    hidden = client.post(
        f"{_PREFIX}/later-items/{item['later_item_id']}/archive",
        json={"expected_version": 2},
        headers=headers,
    )
    assert hidden.status_code == 200
    assert hidden.json()["item"]["status"] == "archived"
    assert client.get(f"{_PREFIX}/later-items", headers=headers).json()["items"] == []
    assert client.patch(
        f"{_PREFIX}/later-items/{item['later_item_id']}",
        json={"content": "越权", "expected_version": 3},
        headers=_headers(intruder["access_token"]),
    ).status_code == 404


def test_button_api_drives_loop_and_preserves_completion_card(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-3", phone="13900011103")
    headers = _headers(bound["access_token"])
    created = _create_task(bound["platform_user_id"])
    plan = created.plans[0]

    selected = client.post(
        f"{_PREFIX}/tasks/{created.task.id}/plans/{plan.id}/select",
        json=_action("select001", created.task.version),
        headers=headers,
    )
    assert selected.status_code == 200
    selected_body = selected.json()
    assert selected_body["state"]["task"]["status"] == "ready"
    assert selected_body["cards"][0]["type"] == "ready_to_start"
    step_id = selected_body["state"]["current_step"]["step_id"]

    started = client.post(
        f"{_PREFIX}/steps/{step_id}/start",
        json=_action("start0001", selected_body["state"]["state_version"]),
        headers=headers,
    )
    assert started.status_code == 200
    started_body = started.json()
    assert started_body["state"]["task"]["status"] == "active"
    assert started_body["cards"][0]["type"] == "active_step"

    completed = client.post(
        f"{_PREFIX}/steps/{step_id}/complete",
        json=_action("complete1", started_body["state"]["state_version"]),
        headers=headers,
    )
    assert completed.status_code == 200
    completed_body = completed.json()
    assert completed_body["state"]["task"]["status"] == "done"
    assert completed_body["cards"][0]["type"] == "completion"
    assert completed_body["cards"][0]["message"] == "你已经完成了这次行动。"

    repeated = client.post(
        f"{_PREFIX}/steps/{step_id}/complete",
        json=_action("complete1", 1),
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["state"]["state_version"] == completed_body["state"]["state_version"]


def test_button_api_rejects_stale_version_and_other_user(client, monkeypatch):
    owner = _bind(client, monkeypatch, openid="openid-app-4", phone="13900011104")
    intruder = _bind(client, monkeypatch, openid="openid-app-5", phone="13900011105")
    created = _create_task(owner["platform_user_id"])
    path = f"{_PREFIX}/tasks/{created.task.id}/plans/{created.plans[0].id}/select"

    forbidden = client.post(
        path,
        json=_action("intruder1"),
        headers=_headers(intruder["access_token"]),
    )
    assert forbidden.status_code == 404
    stale = client.post(
        path,
        json=_action("stale0001", 999),
        headers=_headers(owner["access_token"]),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "task_version_conflict"


def test_abandon_button_and_request_validation(client, monkeypatch):
    bound = _bind(client, monkeypatch, openid="openid-app-6", phone="13900011106")
    headers = _headers(bound["access_token"])
    created = _create_task(bound["platform_user_id"])
    invalid = client.post(
        f"{_PREFIX}/tasks/{created.task.id}/abandon",
        json={"client_request_id": "bad"},
        headers=headers,
    )
    assert invalid.status_code == 422
    abandoned = client.post(
        f"{_PREFIX}/tasks/{created.task.id}/abandon",
        json=_action("abandon01", created.task.version),
        headers=headers,
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["state"]["task"]["status"] == "abandoned"
    assert abandoned.json()["cards"] == []
