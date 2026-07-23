"""Agent Runtime 测试套件。

测试目标：
  Test 1 — agent chat 返回结构化输出
  Test 2 — Legacy /openclaw/turn 不受影响（隔离验证）
  Test 3 — 未知 skill 返回 400
  Test 4 — 无 auth 返回 401
  Test 5 — prompt 隔离：Agent prompt 不含 Legacy 字段
  Test 6 — recommendations 不写数据库
"""
import json
from unittest.mock import patch

import pytest


AUTH_HEADER = {"Authorization": "Bearer test-secret"}

TASK_UNDERSTANDING_RESPONSE = json.dumps({
    "reply": "好的，我们先整理房间吧！",
    "intent": "single_task",
    "tasks": [{"title": "整理房间", "estimated_minutes": 30}],
    "execution_mode": "step_by_step",
    "confidence": 0.95,
    "recommendations": [{"type": "suggest_create_task", "confidence": 0.95}],
})


# ---------------------------------------------------------------------------
# Test 1: Agent chat 返回结构化输出
# ---------------------------------------------------------------------------

def test_agent_chat_returns_structured_output(client):
    """正常请求：返回 reply + understanding + recommendations。"""
    with patch("app.agent_runtime.runner.generate_completion", return_value=TASK_UNDERSTANDING_RESPONSE):
        resp = client.post(
            "/agent/chat",
            json={
                "app": "nooki",
                "user_id": "test_user_001",
                "message": "我要整理房间",
                "context": {},
                "skills": ["task-understanding"],
            },
            headers=AUTH_HEADER,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 核心字段存在
    assert "reply" in body
    assert "understanding" in body
    assert "recommendations" in body

    # 意图理解正确
    assert body["understanding"]["intent"] == "single_task"
    assert len(body["understanding"]["tasks"]) == 1
    assert body["understanding"]["tasks"][0]["title"] == "整理房间"

    # recommendations 是列表（建议语义）
    assert isinstance(body["recommendations"], list)


# ---------------------------------------------------------------------------
# Test 2: Legacy /openclaw/turn 不受影响
# ---------------------------------------------------------------------------

def test_legacy_turn_unaffected(client):
    """/openclaw/turn 仍然正常响应，行为不变。"""
    resp = client.post(
        "/openclaw/turn",
        json={
            "channel": "weixin",
            "openid": "test_openid_001",
            "text": "你好",
        },
        headers={"Authorization": "Bearer test-secret"},
    )
    # 只验证 Legacy 入口存在且有响应（status 不是 404/500），具体行为由其他测试覆盖
    assert resp.status_code != 404, "Legacy /openclaw/turn endpoint must still exist"
    assert resp.status_code < 500, f"Legacy endpoint returned server error: {resp.text}"


# ---------------------------------------------------------------------------
# Test 3: 未知 skill 返回 400
# ---------------------------------------------------------------------------

def test_unknown_skill_returns_400(client):
    """请求不存在的 skill 时返回 400。"""
    resp = client.post(
        "/agent/chat",
        json={
            "app": "nooki",
            "user_id": "test_user_001",
            "message": "测试",
            "skills": ["nonexistent-skill-xyz"],
        },
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 400
    assert "nonexistent-skill-xyz" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 4: 无 auth 返回 401
# ---------------------------------------------------------------------------

def test_agent_chat_requires_auth(client):
    """无 Authorization header 时返回 401。"""
    resp = client.post(
        "/agent/chat",
        json={
            "app": "nooki",
            "user_id": "test_user_001",
            "message": "测试",
            "skills": [],
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test 5: Prompt 隔离 — Agent prompt 不含 Legacy 特有字段
# ---------------------------------------------------------------------------

def test_agent_prompt_does_not_contain_legacy_fields(client):
    """Agent 构建的 prompt 不含 Legacy Chat 的字段（Soul、Long-term Memory 等）。

    验证 Agent Runtime 不污染 Legacy prompt 逻辑。
    """
    captured_messages = []

    def mock_generate_completion(messages, **kwargs):
        captured_messages.extend(messages)
        return TASK_UNDERSTANDING_RESPONSE

    with patch("app.agent_runtime.runner.generate_completion", side_effect=mock_generate_completion):
        resp = client.post(
            "/agent/chat",
            json={
                "app": "nooki",
                "user_id": "test_user_001",
                "message": "我要整理房间",
                "skills": ["task-understanding"],
            },
            headers=AUTH_HEADER,
        )

    assert resp.status_code == 200, resp.text
    assert len(captured_messages) > 0, "generate_completion must have been called"

    # 拼接所有 prompt 内容，检查不含 Legacy 特有字段
    full_prompt = "\n".join(m.get("content", "") for m in captured_messages)

    legacy_markers = [
        "Soul",            # Legacy Soul block
        "Long-term Memory",  # Legacy memory block
        "rolling_summary",   # Legacy rolling summary
        "onboarding",        # Legacy onboarding block
        "tool_instructions", # Legacy tool instructions block
        "openclaw",          # Legacy 渠道标识
    ]
    for marker in legacy_markers:
        assert marker not in full_prompt, (
            f"Agent prompt must not contain Legacy field '{marker}'. "
            f"This indicates pollution of the Legacy prompt pipeline."
        )

    # 验证 Agent 专有字段存在
    assert "Task Understanding" in full_prompt or "task-understanding" in full_prompt.lower()


# ---------------------------------------------------------------------------
# Test 6: Recommendations 不写数据库
# ---------------------------------------------------------------------------

def test_recommendations_do_not_modify_database(client, fresh_db):
    """Agent 返回 recommendations 后，数据库不发生任何变更。

    验证 Agent 的"建议语义"：recommendations 不触发任何执行。
    """
    from app.db import list_sessions  # 用于检查 DB 状态

    # 记录请求前的 sessions 数量（一个代表性指标）
    sessions_before = list_sessions()
    count_before = len(sessions_before)

    with patch("app.agent_runtime.runner.generate_completion", return_value=TASK_UNDERSTANDING_RESPONSE):
        resp = client.post(
            "/agent/chat",
            json={
                "app": "nooki",
                "user_id": "test_user_agent_db_check",
                "message": "我要整理房间",
                "skills": ["task-understanding"],
            },
            headers=AUTH_HEADER,
        )

    assert resp.status_code == 200
    body = resp.json()
    # recommendations 存在
    assert len(body["recommendations"]) > 0

    # 数据库 sessions 数量不变
    sessions_after = list_sessions()
    count_after = len(sessions_after)
    assert count_after == count_before, (
        f"Database was modified by Agent Runtime! "
        f"Sessions before: {count_before}, after: {count_after}. "
        f"recommendations must be suggestions only, not executed."
    )


# ---------------------------------------------------------------------------
# Test 7: output_parser fallback — LLM 返回非 JSON 时不崩溃
# ---------------------------------------------------------------------------

def test_output_parser_fallback_on_invalid_json(client):
    """LLM 返回非 JSON 时，response 有 fallback 而不是 500。"""
    with patch("app.agent_runtime.runner.generate_completion", return_value="我不知道怎么整理"):
        resp = client.post(
            "/agent/chat",
            json={
                "app": "nooki",
                "user_id": "test_user_001",
                "message": "我要整理房间",
                "skills": ["task-understanding"],
            },
            headers=AUTH_HEADER,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "reply" in body
    assert body["metadata"].get("parse_error") is True
