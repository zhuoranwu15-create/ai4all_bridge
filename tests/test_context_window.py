"""app.agent_runtime.context.window 纯函数单测：token 预算裁剪 + 单消息硬上限。"""
from app.agent_runtime.context.window import (
    cap_message_chars,
    estimate_tokens,
    trim_history_rows,
)


def _rows(*contents):
    """构造正序（最旧→最新）历史行；id 单调递增。"""
    return [
        {"id": i + 1, "session_id": 1, "message_id": f"m{i+1}",
         "role": "user" if i % 2 == 0 else "assistant", "content": c}
        for i, c in enumerate(contents)
    ]


def test_estimate_tokens_ceil_len_over_1_5():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 2          # ceil(3/1.5)=2
    assert estimate_tokens("一二三四") == 3      # ceil(4/1.5)=3
    assert estimate_tokens(None) == 0


def test_cap_message_chars_truncates_with_marker():
    out, truncated = cap_message_chars("x" * 10, 4)
    assert truncated is True
    assert out == "xxxx...[已截断]"
    # 未超限不动
    out2, truncated2 = cap_message_chars("short", 100)
    assert (out2, truncated2) == ("short", False)
    # max<=0 表示不限制
    out3, truncated3 = cap_message_chars("x" * 10, 0)
    assert (out3, truncated3) == ("x" * 10, False)


def test_budget_zero_is_noop():
    rows = _rows("a", "b", "c")
    res = trim_history_rows(rows, token_budget=0, per_message_max_chars=0)
    assert res["kept"] == rows
    assert res["dropped"] == []
    assert res["metrics"]["dropped_count"] == 0


def test_budget_drops_oldest_keeps_tail():
    # 每条 content 3 字 → est 2 token/条；预算 4 → 只能容 2 条，丢最旧 1 条。
    rows = _rows("aaa", "bbb", "ccc")
    res = trim_history_rows(rows, token_budget=4, per_message_max_chars=0)
    kept_contents = [r["content"] for r in res["kept"]]
    assert kept_contents == ["bbb", "ccc"]        # 保留尾部最近
    assert [r["content"] for r in res["dropped"]] == ["aaa"]
    assert res["metrics"]["dropped_count"] == 1


def test_budget_keeps_at_least_one_message():
    # 单条就超预算时，至少保留最后 1 条（不丢空）。
    rows = _rows("x" * 30, "y" * 30)
    res = trim_history_rows(rows, token_budget=1, per_message_max_chars=0)
    assert len(res["kept"]) == 1
    assert res["kept"][0]["content"] == "y" * 30


def test_per_message_cap_then_budget():
    # 先截断超长单条，再计预算。截断后每条变短，可能就不再触发丢弃。
    rows = _rows("z" * 100, "ok")
    res = trim_history_rows(rows, token_budget=100, per_message_max_chars=5)
    assert res["metrics"]["truncated_count"] == 1
    assert res["kept"][0]["content"] == "zzzzz...[已截断]"
    # 入参对象不被修改（拷贝语义）
    assert rows[0]["content"] == "z" * 100


def test_does_not_mutate_input_rows():
    rows = _rows("a" * 50)
    trim_history_rows(rows, token_budget=0, per_message_max_chars=3)
    assert rows[0]["content"] == "a" * 50


def test_empty_history():
    res = trim_history_rows([], token_budget=100, per_message_max_chars=10)
    assert res["kept"] == []
    assert res["dropped"] == []
    assert res["metrics"]["kept_count"] == 0
