"""TDAI 主动检索工具 handler 单元测试。

覆盖：per-turn 限流（两工具共用计数）、account_id 取自 ctx、空 query 拒绝、
client 空结果降级为 benign。不触达网络（patch where it's used）。
"""
from types import SimpleNamespace

import pytest

from app.tools import tdai_search_handlers as h


def _ctx(account_id="aid_806382741", calls=0):
    return SimpleNamespace(account_id=account_id, tdai_search_calls=calls)


def test_memory_search_success_passthrough(monkeypatch):
    def _fake(*, account_id, query, limit, type=None, scene=None):
        return {"results": "偏好：简短回复", "total": 1, "strategy": "hybrid"}

    monkeypatch.setattr(h, "search_memories", _fake)
    ctx = _ctx()
    out = h.handle_tdai_memory_search({"query": "回复长度偏好"}, ctx)
    assert out["status"] == "succeeded"
    assert out["results"] == "偏好：简短回复"
    assert out["total"] == 1 and out["strategy"] == "hybrid"
    assert ctx.tdai_search_calls == 1  # 计数自增


def test_account_id_taken_from_ctx_not_args(monkeypatch):
    """隔离要求：忽略 args 里的 account_id，一律用 ctx.account_id。"""
    seen = {}

    def _fake(*, account_id, query, limit):
        seen["account_id"] = account_id
        return {"results": "x", "total": 1}

    monkeypatch.setattr(h, "search_conversations", _fake)
    h.handle_tdai_conversation_search(
        {"query": "上次原话", "account_id": "aid_ATTACKER"}, _ctx(account_id="aid_806382741")
    )
    assert seen["account_id"] == "aid_806382741"


def test_empty_query_rejected(monkeypatch):
    monkeypatch.setattr(h, "search_memories", lambda **k: {"results": "should not reach"})
    ctx = _ctx()
    out = h.handle_tdai_memory_search({"query": "   "}, ctx)
    assert out["status"] == "failed" and "query" in out["error"]


def test_empty_result_degrades_benign(monkeypatch):
    """client 返回 {}（禁用/超时/失败）→ benign 成功结果，不打断生成。"""
    monkeypatch.setattr(h, "search_memories", lambda **k: {})
    out = h.handle_tdai_memory_search({"query": "q"}, _ctx())
    assert out["status"] == "succeeded"
    assert out["results"] == h._EMPTY_RESULT
    assert out["total"] == 0


def test_per_turn_limit_shared_across_both_tools(monkeypatch):
    """两工具合计每轮 3 次；第 4 次（不论哪个工具）被拒且不再调 client。"""
    monkeypatch.setattr(h.settings, "tdai_search_max_calls_per_turn", 3, raising=False)
    calls = {"n": 0}

    def _fake_mem(**k):
        calls["n"] += 1
        return {"results": "m", "total": 1}

    def _fake_conv(**k):
        calls["n"] += 1
        return {"results": "c", "total": 1}

    monkeypatch.setattr(h, "search_memories", _fake_mem)
    monkeypatch.setattr(h, "search_conversations", _fake_conv)

    ctx = _ctx()
    assert h.handle_tdai_memory_search({"query": "1"}, ctx)["status"] == "succeeded"
    assert h.handle_tdai_conversation_search({"query": "2"}, ctx)["status"] == "succeeded"
    assert h.handle_tdai_memory_search({"query": "3"}, ctx)["status"] == "succeeded"
    # 第 4 次：合计已达 3，拒绝
    out4 = h.handle_tdai_conversation_search({"query": "4"}, ctx)
    assert out4["status"] == "failed" and "上限" in out4["error"]
    assert calls["n"] == 3  # client 只被调了 3 次
    assert ctx.tdai_search_calls == 3  # 被拒的第 4 次不自增


def test_limit_arg_clamped(monkeypatch):
    seen = {}

    def _fake(*, account_id, query, limit, type=None, scene=None):
        seen["limit"] = limit
        return {"results": "x", "total": 1}

    monkeypatch.setattr(h, "search_memories", _fake)
    h.handle_tdai_memory_search({"query": "q", "limit": 999}, _ctx())
    assert seen["limit"] == 20  # 夹到上限
    h.handle_tdai_memory_search({"query": "q", "limit": "bad"}, _ctx())
    assert seen["limit"] == 5  # 非法取默认
