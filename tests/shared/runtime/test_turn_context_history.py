from unittest.mock import patch


def _add_message(account_id: str, session_id: int, message_id: str, role: str, content: str) -> None:
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


def _session(account_id: str, session_key: str, carryover_summary: str = "") -> dict:
    from app.db import get_or_create_session

    return get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=session_key,
        carryover_summary=carryover_summary or None,
    )


def test_history_is_session_scoped_not_cross_session(fresh_db):
    """统一编排：L0 原始尾窗 session-scoped——上一段 session 的原文不再进 history。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-session-scoped"
    previous = _session(account_id, "previous")
    current = _session(account_id, "current")
    _add_message(account_id, previous["session"]["id"], "prev-u", "user", "上一段消息")
    _add_message(account_id, previous["session"]["id"], "prev-a", "assistant", "上一段回复")
    _add_message(account_id, current["session"]["id"], "cur-u1", "user", "本段较早消息")
    _add_message(account_id, current["session"]["id"], "cur-a1", "assistant", "本段助手回复")
    _add_message(account_id, current["session"]["id"], "cur-u2", "user", "当前消息")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_history_timestamp_enabled = False  # 隔离：不引入时间戳前缀，便于精确断言原文
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=current["session"],
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    # 只含当前 session 的消息，上一段完全不出现。
    assert [message["content"] for message in llm_input["history"]] == [
        "本段较早消息",
        "本段助手回复",
        "当前消息",
    ]
    assert llm_input["metadata"]["history_cross_session"] is False
    assert llm_input["metadata"]["history_session_count"] == 1
    # 无独立 carryover block。
    assert "【会话延续摘要】" not in llm_input["system_prompt"]


def test_rolling_summary_is_the_single_summary_block(fresh_db):
    """统一编排：carryover seed 进 rolling 后，prompt 只出现唯一【更早对话摘要】，不出现【会话延续摘要】。"""
    from app.turn_service import build_turn_llm_input
    from app.db import get_session, update_session_rolling_summary

    account_id = "acc-single-block"
    current = _session(account_id, "current", carryover_summary="dreaming 产出的 carryover 源文")
    # 模拟轮转时的 seed：新 session rolling_summary := carryover（水位线从 0 起）。
    update_session_rolling_summary(
        session_id=int(current["session"]["id"]),
        rolling_summary="上一段延续摘要（作为本段 rolling 的 seed）",
        rolling_summary_upto_id=0,
    )
    _add_message(account_id, current["session"]["id"], "cur-u", "user", "今天消息")
    sess = get_session(session_id=int(current["session"]["id"]))  # 取到 seed 后的 rolling_summary

    fresh_db.llm_context_messages = 100
    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=sess,
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    sysp = llm_input["system_prompt"]
    assert "【更早对话摘要】" in sysp
    assert "上一段延续摘要（作为本段 rolling 的 seed）" in sysp
    assert "【会话延续摘要】" not in sysp                 # carryover 不再单独注入
    assert llm_input["metadata"]["rolling_summary_included"] is True
    assert llm_input["metadata"]["carryover_summary_included"] is False


def test_watermark_invariant_folds_below_keeps_above_over_budget(fresh_db):
    """水位线不变量（rolling 开）：水位线**之后**的原文全部保留（哪怕超预算、不丢），
    水位线**及之前**的折叠进摘要、不在 history —— 关掉「已丢窗口但未进摘要」的信息缺口。"""
    from app.turn_service import build_turn_llm_input
    from app.db import (
        get_session,
        update_session_rolling_summary,
        list_recent_context_messages_for_session,
    )

    account_id = "acc-watermark-inv"
    current = _session(account_id, "current")
    sid = int(current["session"]["id"])
    for i in range(5):
        _add_message(account_id, sid, f"m{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}内容")
    rows = list_recent_context_messages_for_session(
        session_id=sid,
        account_id=account_id,
        limit=100,
    )
    watermark = int(rows[1]["id"])  # 前 2 条视为已进摘要
    update_session_rolling_summary(
        session_id=sid, rolling_summary="早期对话摘要", rolling_summary_upto_id=watermark
    )
    sess = get_session(session_id=sid)

    fresh_db.llm_rolling_summary_enabled = True
    # raw(3 条×4≈12 token) > 预算 8，但 < 2×预算硬顶(16) → 水位线之后全保留、一条不丢。
    fresh_db.llm_context_token_budget = 8
    fresh_db.llm_context_message_max_chars = 0
    fresh_db.llm_context_floor_minutes = 0
    fresh_db.llm_context_floor_turns = 0
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=sess,
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    # 水位线之后的 3 条全部保留（哪怕 raw 超预算）；前 2 条折叠、不在 history。
    assert [m["content"] for m in llm_input["history"]] == ["消息2内容", "消息3内容", "消息4内容"]
    assert llm_input["metadata"]["history_watermark_upto_id"] == watermark
    assert llm_input["metadata"]["history_dropped_count"] == 0
    # 折叠部分由单一【更早对话摘要】覆盖。
    assert "【更早对话摘要】" in llm_input["system_prompt"]
    assert "早期对话摘要" in llm_input["system_prompt"]


def test_watermark_invariant_keeps_all_above_watermark_beyond_fetch_cap(fresh_db):
    """P1#2：水位线之后的未摘要消息即使条数超过旧的 llm_context_messages 上限，也一条不丢
    （按 after_id=水位线 取其后全部，而非"最近 N 条"）。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-many-small"
    current = _session(account_id, "current")
    sid = int(current["session"]["id"])
    # 30 条短消息（水位线=0，全部未摘要），远超取数上限 llm_context_messages=5。
    for i in range(30):
        _add_message(account_id, sid, f"m{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}")

    fresh_db.llm_rolling_summary_enabled = True
    fresh_db.llm_context_messages = 5          # 旧口径下会只取最近 5 条 → 丢掉最老 25 条
    fresh_db.llm_context_token_budget = 100000  # 预算极大，不触发任何裁剪
    fresh_db.llm_context_floor_minutes = 0
    fresh_db.llm_context_floor_turns = 0
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=current["session"],
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    # 全部 30 条都在（水位线之后全取），不受 llm_context_messages=5 影响。
    assert len(llm_input["history"]) == 30
    assert llm_input["history"][0]["content"] == "消息0"
    assert llm_input["history"][-1]["content"] == "消息29"


def test_token_budget_drops_old_session_messages_when_floor_off(fresh_db):
    """关硬底时，token 预算从最旧端丢弃本 session 的旧消息，只保留尾部最近。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-token-budget"
    current = _session(account_id, "current")
    _add_message(account_id, current["session"]["id"], "cur-u1", "user", "较早消息")
    _add_message(account_id, current["session"]["id"], "cur-a1", "assistant", "较早回复")
    _add_message(account_id, current["session"]["id"], "cur-u2", "user", "当前消息")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_context_token_budget = 4          # 每条 ~4 token → 只容最后 1 条
    fresh_db.llm_context_message_max_chars = 0
    fresh_db.llm_context_floor_minutes = 0         # 关硬底，单验预算裁剪
    fresh_db.llm_context_floor_turns = 0
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=current["session"],
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    assert [m["content"] for m in llm_input["history"]] == ["当前消息"]
    assert llm_input["metadata"]["history_dropped_count"] == 2


def test_floor_keeps_recent_messages_over_token_budget(fresh_db):
    """硬底优先：15min/10 轮内的原文即使远超 token 预算也不被丢弃。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-floor"
    current = _session(account_id, "current")
    # 三条都是刚插入的（created_at≈now，均在 15 分钟内）。
    _add_message(account_id, current["session"]["id"], "u1", "user", "较早消息")
    _add_message(account_id, current["session"]["id"], "a1", "assistant", "较早回复")
    _add_message(account_id, current["session"]["id"], "u2", "user", "当前消息")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_context_token_budget = 1          # 预算极小
    fresh_db.llm_context_message_max_chars = 0
    fresh_db.llm_context_floor_minutes = 15        # 硬底默认口径
    fresh_db.llm_context_floor_turns = 10
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=current["account"],
            session=current["session"],
            profile={},
            text="",
            today="2026-06-03",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    # 硬底覆盖全部 3 条 → 一条不丢，尽管预算=1。
    assert [m["content"] for m in llm_input["history"]] == ["较早消息", "较早回复", "当前消息"]
    assert llm_input["metadata"]["history_dropped_count"] == 0
    assert llm_input["metadata"]["history_floor_count"] == 3


def test_per_message_char_cap_truncates_history_feed(fresh_db):
    """单条历史消息超 char 上限时在喂 LLM 副本里截断加标记（不改落库）。"""
    from app.turn_service import build_turn_llm_input
    from app.db import list_recent_messages_for_account

    account_id = "acc-msg-cap"
    sess = _session(account_id, "s1")
    long_text = "今天用户消息" * 5  # 30 字
    _add_message(account_id, sess["session"]["id"], "u1", "user", long_text)

    fresh_db.llm_context_messages = 100
    fresh_db.llm_context_token_budget = 0          # 隔离截断，不做预算丢弃
    fresh_db.llm_context_message_max_chars = 5
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=sess["account"],
            session=sess["session"],
            profile={},
            text="",
            today="2026-06-24",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    assert llm_input["history"][0]["content"] == "今天用户消" + "...[已截断]"
    assert llm_input["metadata"]["history_truncated_count"] == 1
    # 落库内容不受影响。
    rows = list_recent_messages_for_account(account_id=account_id, limit=10)
    assert rows[0]["content"] == long_text


def test_format_history_timestamp_pure():
    """纯函数：北京裸串/带 T 的 ISO/带微秒/datetime 都渲染为 `周X YYYY-MM-DD HH:MM`；脏值返回空串。"""
    from datetime import datetime
    from app.time_utils import format_history_timestamp

    # 2026-07-06 是周一。
    assert format_history_timestamp("2026-07-06 11:39:07") == "周一 2026-07-06 11:39"
    assert format_history_timestamp("2026-07-06T11:39:07") == "周一 2026-07-06 11:39"
    assert format_history_timestamp("2026-07-06 11:39:07.123456") == "周一 2026-07-06 11:39"
    assert format_history_timestamp(datetime(2026, 7, 6, 11, 39, 7)) == "周一 2026-07-06 11:39"
    # 脏值/空值兜底：不抛异常、返回空串（调用方据此不加前缀）。
    assert format_history_timestamp("") == ""
    assert format_history_timestamp(None) == ""
    assert format_history_timestamp("not-a-date") == ""


def test_history_timestamp_prefixes_past_user_turns_only(fresh_db):
    """历史 user 轮盖 `[周X ...]` 前缀；assistant 与当前轮不盖；落库不变。"""
    from app.turn_service import build_turn_llm_input
    from app.db import list_recent_messages_for_account

    account_id = "acc-history-ts"
    sess = _session(account_id, "s1")
    _add_message(account_id, sess["session"]["id"], "u1", "user", "早些的用户消息")
    _add_message(account_id, sess["session"]["id"], "a1", "assistant", "助手回复")
    _add_message(account_id, sess["session"]["id"], "u2", "user", "当前用户消息")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_history_timestamp_enabled = True
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=sess["account"],
            session=sess["session"],
            profile={},
            text="",
            today="2026-07-06",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    history = llm_input["history"]
    # 历史 user 轮：带前缀、原文在末尾。
    assert history[0]["content"].startswith("[周")
    assert history[0]["content"].endswith("\n早些的用户消息")
    # assistant 轮：不带前缀。
    assert history[1]["content"] == "助手回复"
    # 当前轮（最后一条 user）：不带前缀（由 <current_message> + 运行时块覆盖 now）。
    assert history[2]["content"] == "当前用户消息"
    assert llm_input["metadata"]["history_timestamped_count"] == 1
    # 落库 content 不受影响。
    rows = list_recent_messages_for_account(account_id=account_id, limit=10)
    assert [r["content"] for r in rows] == ["早些的用户消息", "助手回复", "当前用户消息"]


def test_history_timestamp_disabled_leaves_history_untouched(fresh_db):
    """开关关闭时历史逐字不变（回滚闸）。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-history-ts-off"
    sess = _session(account_id, "s1")
    _add_message(account_id, sess["session"]["id"], "u1", "user", "早些的用户消息")
    _add_message(account_id, sess["session"]["id"], "u2", "user", "当前用户消息")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_history_timestamp_enabled = False
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=sess["account"],
            session=sess["session"],
            profile={},
            text="",
            today="2026-07-06",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=False,
            include_tool_instructions=False,
        )

    assert [m["content"] for m in llm_input["history"]] == ["早些的用户消息", "当前用户消息"]
    assert llm_input["metadata"]["history_timestamped_count"] == 0


def test_tool_surface_block_present_when_web_search_enabled(fresh_db):
    """web_search_enabled=True 时 system prompt 应含「本轮可用工具」block 且列出 web_search。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-tool-surface"
    sess = _session(account_id, "s1")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_tool_surface_prompt_enabled = True
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=sess["account"],
            session=sess["session"],
            profile={},
            text="",
            today="2026-06-24",
            onboarding_state="complete",
            onboarding_active=False,
            web_search_enabled=True,
            include_tool_instructions=False,
        )

    assert "【本轮可用工具】" in llm_input["system_prompt"]
    assert "web_search" in llm_input["system_prompt"]
    assert "TOOLS.md" in llm_input["system_prompt"]


def test_tool_surface_block_absent_during_onboarding(fresh_db):
    """onboarding_active=True 时 system prompt 不含工具 block（onboarding 阶段无工具）。"""
    from app.turn_service import build_turn_llm_input

    account_id = "acc-tool-surface-onboarding"
    sess = _session(account_id, "s1")

    fresh_db.llm_context_messages = 100
    fresh_db.llm_tool_surface_prompt_enabled = True
    with patch("app.turn_service.settings", fresh_db):
        llm_input = build_turn_llm_input(
            account_id=account_id,
            account=sess["account"],
            session=sess["session"],
            profile={},
            text="",
            today="2026-06-24",
            onboarding_state="step1",
            onboarding_active=True,
            web_search_enabled=True,
            include_tool_instructions=False,
        )

    assert "【本轮可用工具】" not in llm_input["system_prompt"]
