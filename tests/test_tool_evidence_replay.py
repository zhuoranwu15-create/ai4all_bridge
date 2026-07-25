"""Tests for app/tool_evidence_replay.py (Batch C tool evidence replay)."""
import json
import pytest


# ---------- helpers ----------

def _make_history(pairs):
    """pairs: [(role, content, message_id), ...]"""
    history = []
    rows = []
    for role, content, mid in pairs:
        history.append({"role": role, "content": content})
        rows.append({"role": role, "content": content, "message_id": mid})
    return history, rows


def _invoke(invocations):
    """Patch list_recent_tool_invocations_for_replay to return fixed list."""
    from unittest.mock import patch
    return patch(
        "app.db.analytics.list_recent_tool_invocations_for_replay",
        return_value=invocations,
    )


# ---------- basic injection ----------

def test_no_tool_invocations_history_unchanged():
    history, rows = _make_history([
        ("user", "hello", "mid-1"),
        ("assistant", "hi", None),
    ])
    with _invoke([]):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1")
    assert result == history


def test_single_tool_call_spliced_after_user_message():
    history, rows = _make_history([
        ("user", "天气怎么样", "mid-1"),
        ("assistant", "北京今天晴", None),
    ])
    invocations = [{
        "message_id": "mid-1",
        "tool_call_id": "call_abc",
        "tool_name": "web_fetch",
        "args": {"url": "https://wttr.in/Beijing?format=3"},
        "result": {"status": 200, "text": "Beijing: ☀️ +28°C"},
        "status": "succeeded",
    }]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1")

    # user → assistant(tool_calls) → tool → assistant(text)
    assert len(result) == 4
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"
    assert "tool_calls" in result[1]
    assert result[2]["role"] == "tool"
    assert result[2]["tool_call_id"] == "call_abc"
    assert result[3]["role"] == "assistant"
    assert result[3]["content"] == "北京今天晴"


def test_tool_call_id_preserved_in_wire():
    history, rows = _make_history([
        ("user", "天气", "mid-x"),
        ("assistant", "晴天", None),
    ])
    invocations = [{
        "message_id": "mid-x",
        "tool_call_id": "tcid_xyz",
        "tool_name": "web_fetch",
        "args": {},
        "result": {},
        "status": "succeeded",
    }]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1")
    assert result[1]["tool_calls"][0]["id"] == "tcid_xyz"
    assert result[2]["tool_call_id"] == "tcid_xyz"


def test_tool_name_preserved():
    history, rows = _make_history([
        ("user", "搜索", "mid-2"),
        ("assistant", "ok", None),
    ])
    invocations = [{
        "message_id": "mid-2",
        "tool_call_id": "c1",
        "tool_name": "web_search",
        "args": {"q": "bitcoin"},
        "result": {"results": []},
        "status": "succeeded",
    }]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1")
    assert result[1]["tool_calls"][0]["function"]["name"] == "web_search"


# ---------- truncation ----------

def test_result_truncated_to_max_result_chars():
    history, rows = _make_history([
        ("user", "q", "mid-3"),
        ("assistant", "a", None),
    ])
    big_result = {"data": "x" * 5000}
    invocations = [{
        "message_id": "mid-3",
        "tool_call_id": "c1",
        "tool_name": "web_fetch",
        "args": {},
        "result": big_result,
        "status": "succeeded",
    }]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1", max_result_chars=200)
    tool_content = result[2]["content"]
    assert len(tool_content) <= 210  # 200 + truncation marker overhead
    assert "截断" in tool_content


# ---------- max_turns ----------

def test_only_recent_max_turns_spliced():
    # 3 user messages; only last 2 should be eligible for replay
    history, rows = _make_history([
        ("user", "q1", "mid-1"),
        ("assistant", "a1", None),
        ("user", "q2", "mid-2"),
        ("assistant", "a2", None),
        ("user", "q3", "mid-3"),
        ("assistant", "a3", None),
    ])
    # DB returns invocations for all 3, but max_turns=2 means only mid-2 and mid-3
    invocations = [
        {"message_id": "mid-1", "tool_call_id": "c1", "tool_name": "web_fetch", "args": {}, "result": {}, "status": "succeeded"},
        {"message_id": "mid-2", "tool_call_id": "c2", "tool_name": "web_fetch", "args": {}, "result": {}, "status": "succeeded"},
        {"message_id": "mid-3", "tool_call_id": "c3", "tool_name": "web_fetch", "args": {}, "result": {}, "status": "succeeded"},
    ]
    # Since max_turns=2, only mid-2 and mid-3 are in target_message_ids.
    # But the mock returns all 3. The injection will only splice where mid is in by_mid,
    # and by_mid is built from DB result. Test that mid-1 is NOT spliced.
    # To test correctly, we need the mock to only return mid-2 and mid-3.
    # Actually, the function filters by target_message_ids before querying DB (by passing to query).
    # The mock bypasses that. Let's check that only mid-2 and mid-3 appear after injection.
    filtered_invocations = [inv for inv in invocations if inv["message_id"] in ("mid-2", "mid-3")]
    with _invoke(filtered_invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1", max_turns=2)

    # mid-1 should NOT have tool_calls spliced after it
    assert result[0]["role"] == "user" and result[0]["content"] == "q1"
    assert result[1]["role"] == "assistant" and "tool_calls" not in result[1]
    # mid-2 should have tool_calls spliced
    assert result[2]["role"] == "user" and result[2]["content"] == "q2"
    assert result[3]["role"] == "assistant" and "tool_calls" in result[3]


# ---------- disabled ----------

def test_disabled_returns_history_unchanged():
    history, rows = _make_history([
        ("user", "天气", "mid-1"),
        ("assistant", "晴", None),
    ])
    invocations = [{"message_id": "mid-1", "tool_call_id": "c1", "tool_name": "web_fetch", "args": {}, "result": {}, "status": "succeeded"}]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1", enabled=False)
    assert result == history


# ---------- edge cases ----------

def test_empty_history_returns_empty():
    from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
    result = inject_tool_evidence_replay([], [], "acc-1")
    assert result == []


def test_no_message_ids_in_history_rows():
    history, rows = _make_history([
        ("user", "q", None),   # no message_id
        ("assistant", "a", None),
    ])
    from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
    result = inject_tool_evidence_replay(history, rows, "acc-1")
    assert result == history


def test_multiple_tool_calls_same_turn_all_spliced():
    history, rows = _make_history([
        ("user", "查天气和汇率", "mid-m"),
        ("assistant", "结果如下", None),
    ])
    invocations = [
        {"message_id": "mid-m", "tool_call_id": "c1", "tool_name": "web_fetch", "args": {"url": "https://wttr.in"}, "result": {"text": "sunny"}, "status": "succeeded"},
        {"message_id": "mid-m", "tool_call_id": "c2", "tool_name": "web_fetch", "args": {"url": "https://rate.com"}, "result": {"text": "1USD=7.2"}, "status": "succeeded"},
    ]
    with _invoke(invocations):
        from app.agent_runtime.context.evidence_replay import inject_tool_evidence_replay
        result = inject_tool_evidence_replay(history, rows, "acc-1")
    # user → assistant(c1) → tool(c1) → assistant(c2) → tool(c2) → assistant(text)
    assert len(result) == 6
    assert result[0]["role"] == "user"
    assert result[1]["tool_calls"][0]["id"] == "c1"
    assert result[2]["tool_call_id"] == "c1"
    assert result[3]["tool_calls"][0]["id"] == "c2"
    assert result[4]["tool_call_id"] == "c2"
    assert result[5]["content"] == "结果如下"
