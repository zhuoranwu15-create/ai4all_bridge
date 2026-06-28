"""P3 迁移 schema + 滚动摘要后台逻辑测试（默认关；LLM 未配置时走确定性兜底）。"""
from unittest.mock import patch


def _add_message(account_id, session_id, message_id, role, content):
    from app.db import insert_message

    insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound" if role == "user" else "outbound",
        role=role,
        message_type="text",
        content=content,
        raw={},
    )


def _session(account_id, session_key):
    from app.db import get_or_create_session

    return get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=session_key,
    )


# ---------------------------------------------------------------------------
# 迁移 schema
# ---------------------------------------------------------------------------

def test_sessions_rolling_columns_exist(fresh_db):
    from app.db import connect

    with connect() as conn:
        # 两后端通用：列不存在则抛错，测试即失败。
        conn.execute("SELECT rolling_summary, rolling_summary_upto_id FROM sessions LIMIT 0")


def test_messages_account_id_index_exists(fresh_db):
    from app.db import connect
    from app.db._backend import is_postgres

    with connect() as conn:
        if is_postgres():
            rows = conn.execute(
                "SELECT indexname AS name FROM pg_indexes WHERE tablename = 'messages'"
            ).fetchall()
        else:
            rows = conn.execute("PRAGMA index_list('messages')").fetchall()
        names = {row["name"] for row in rows}
    assert "ix_messages_account_id" in names


# ---------------------------------------------------------------------------
# 滚动摘要后台逻辑
# ---------------------------------------------------------------------------

def test_rolling_summary_disabled_by_default(fresh_db):
    from app.context_summarizer import maybe_update_rolling_summary

    # 默认 enabled=False
    with patch("app.context_summarizer.settings", fresh_db):
        result = maybe_update_rolling_summary(account_id="acc", session_id=1)
    assert result["status"] == "disabled"


def test_rolling_summary_generates_when_overflow(fresh_db):
    from app.context_summarizer import maybe_update_rolling_summary
    from app.db import get_session

    account_id = "acc-rolling"
    sess = _session(account_id, "s1")
    session_id = int(sess["session"]["id"])
    for i in range(12):
        _add_message(account_id, session_id, f"m{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}")

    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_context_messages = 5          # keep_recent=5 → 滑出 7 条
    fresh_db.llm_rolling_summary_trigger_messages = 3
    # LLM 未配置 → _summarize 走确定性兜底，无外部调用。
    with patch("app.context_summarizer.settings", fresh_db), \
         patch("app.llm.settings", fresh_db):
        result = maybe_update_rolling_summary(account_id=account_id, session_id=session_id)

    assert result["status"] == "updated"
    assert result["summarized"] == 7
    updated = get_session(session_id=session_id)
    assert (updated.get("rolling_summary") or "").strip() != ""
    assert int(updated.get("rolling_summary_upto_id") or 0) > 0


def test_rolling_summary_skips_below_window(fresh_db):
    from app.context_summarizer import maybe_update_rolling_summary

    account_id = "acc-rolling-small"
    sess = _session(account_id, "s1")
    session_id = int(sess["session"]["id"])
    for i in range(3):
        _add_message(account_id, session_id, f"m{i}", "user", f"消息{i}")

    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_context_messages = 100        # 远大于消息数 → 无溢出
    with patch("app.context_summarizer.settings", fresh_db):
        result = maybe_update_rolling_summary(account_id=account_id, session_id=session_id)
    assert result["status"] == "skip"
    assert result["reason"] == "below_window"


def test_rolling_summary_covers_token_budget_dropped_messages(fresh_db):
    """P2-①回归：条数未超 llm_context_messages，但被 token 预算裁掉的消息仍应进摘要。

    旧实现按 llm_context_messages 条数判 below_window（10<=100 → 跳过），漏掉被 token
    预算丢弃的 9 条。新实现用 trim_history_rows 同口径分界，应正常生成摘要。
    """
    from app.context_summarizer import maybe_update_rolling_summary
    from app.db import get_session

    account_id = "acc-budget-overflow"
    sess = _session(account_id, "s1")
    session_id = int(sess["session"]["id"])
    for i in range(10):
        _add_message(account_id, session_id, f"m{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}")

    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_context_messages = 100        # 条数窗口远大于消息数：旧逻辑必判 below_window
    fresh_db.llm_context_token_budget = 2      # token 预算极小 → live window 只剩最后 1 条
    fresh_db.llm_context_message_max_chars = 0
    fresh_db.llm_rolling_summary_trigger_messages = 3
    with patch("app.context_summarizer.settings", fresh_db), \
         patch("app.llm.settings", fresh_db):
        result = maybe_update_rolling_summary(account_id=account_id, session_id=session_id)

    assert result["status"] == "updated"
    assert result["summarized"] == 9          # 10 条里最后 1 条留窗内，前 9 条溢出 → 摘要
    updated = get_session(session_id=session_id)
    assert (updated.get("rolling_summary") or "").strip() != ""


def test_rolling_summary_pages_from_watermark(fresh_db):
    """P2-②回归：候选查询应以 after_id=水位线 分页（而非固定 after_id=0）。

    旧实现固定 after_id=0 + limit=1000，session 超 1000 条后只取头 1000 条、水位线卡死。
    这里用 spy 断言查询确实从已有水位线起分页，直接验证根因修复。
    """
    from app.context_summarizer import maybe_update_rolling_summary
    from app.db import update_session_rolling_summary
    import app.context_summarizer as cs

    account_id = "acc-watermark-paging"
    sess = _session(account_id, "s1")
    session_id = int(sess["session"]["id"])
    for i in range(6):
        _add_message(account_id, session_id, f"m{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}")
    # 预置水位线到第 3 条：后续查询应从 after_id=3 起，不应重取头部已摘要消息。
    update_session_rolling_summary(
        session_id=session_id, rolling_summary="旧摘要", rolling_summary_upto_id=3
    )

    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_context_messages = 100
    fresh_db.llm_context_token_budget = 2      # live window 只剩最后 1 条 → 前面均溢出
    fresh_db.llm_context_message_max_chars = 0
    fresh_db.llm_rolling_summary_trigger_messages = 1

    seen_after_ids = []
    real_fn = cs.list_context_messages_for_session

    def _spy(*, session_id, after_id=0, limit=1000):
        seen_after_ids.append(after_id)
        return real_fn(session_id=session_id, after_id=after_id, limit=limit)

    with patch("app.context_summarizer.settings", fresh_db), \
         patch("app.llm.settings", fresh_db), \
         patch.object(cs, "list_context_messages_for_session", _spy):
        result = maybe_update_rolling_summary(account_id=account_id, session_id=session_id)

    assert seen_after_ids == [3]               # 从水位线分页，非 0
    assert result["status"] == "updated"
    # 仅 id∈(3, window_oldest_id) 的消息入摘要：第 4、5 条（第 6 条留窗内）。
    assert result["summarized"] == 2


def test_rolling_summary_rejects_cross_account(fresh_db):
    """账号隔离：session 不归属传入 account_id 时拒绝。"""
    from app.context_summarizer import maybe_update_rolling_summary

    sess = _session("acc-owner", "s1")
    session_id = int(sess["session"]["id"])
    fresh_db.llm_rolling_summary_enabled = True
    with patch("app.context_summarizer.settings", fresh_db):
        result = maybe_update_rolling_summary(account_id="acc-other", session_id=session_id)
    assert result["status"] == "skip"
    assert result["reason"] == "session_mismatch"
