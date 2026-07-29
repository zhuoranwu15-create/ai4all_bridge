from fastapi.testclient import TestClient

from app.agent_runtime import legacy_chat
from app.main import app


def test_legacy_agent_chat_route_and_skill_loading(monkeypatch):
    monkeypatch.setattr(
        legacy_chat,
        "generate_completion",
        lambda messages: '{"reply":"可以，先收一件东西。","actions":[{"type":"none"}],"recommendations":[]}',
    )
    response = TestClient(app).post(
        "/agent/chat",
        headers={"Authorization": "Bearer dev-secret"},
        json={
            "app": "nooki",
            "user_id": "test-user",
            "message": "我想整理房间",
            "context": {},
            "skills": ["task-understanding"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "可以，先收一件东西。"
    assert body["understanding"]["actions"] == [{"type": "none"}]


def test_legacy_agent_chat_rejects_unknown_skill():
    response = TestClient(app).post(
        "/agent/chat",
        headers={"Authorization": "Bearer dev-secret"},
        json={
            "app": "nooki",
            "user_id": "test-user",
            "message": "hello",
            "context": {},
            "skills": ["../not-allowed"],
        },
    )
    assert response.status_code == 400
