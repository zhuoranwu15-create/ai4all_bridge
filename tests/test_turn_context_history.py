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


def test_turn_history_crosses_sessions_and_suppresses_redundant_carryover(fresh_db):
    from app.turn_service import build_turn_llm_input

    account_id = "acc-cross-session"
    previous = _session(account_id, "previous")
    current = _session(account_id, "current", carryover_summary="上一段 session 的摘要")
    _add_message(account_id, previous["session"]["id"], "prev-u", "user", "昨天用户消息")
    _add_message(account_id, previous["session"]["id"], "prev-a", "assistant", "昨天助手回复")
    _add_message(account_id, current["session"]["id"], "cur-u", "user", "今天用户消息")

    fresh_db.llm_context_messages = 100
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

    assert [message["content"] for message in llm_input["history"]] == [
        "昨天用户消息",
        "昨天助手回复",
        "今天用户消息",
    ]
    assert "【会话延续摘要】" not in llm_input["system_prompt"]
    assert llm_input["metadata"]["history_session_count"] == 2
    assert llm_input["metadata"]["carryover_summary_suppressed_by_history"] is True


def test_turn_history_keeps_carryover_when_previous_session_is_partial(fresh_db):
    from app.turn_service import build_turn_llm_input

    account_id = "acc-partial-history"
    previous = _session(account_id, "previous")
    current = _session(account_id, "current", carryover_summary="上一段 session 的摘要")
    for index in range(5):
        _add_message(
            account_id,
            previous["session"]["id"],
            f"prev-{index}",
            "user" if index % 2 == 0 else "assistant",
            f"昨天消息 {index}",
        )
    _add_message(account_id, current["session"]["id"], "cur-u", "user", "今天用户消息")

    fresh_db.llm_context_messages = 3
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

    assert [message["content"] for message in llm_input["history"]] == [
        "昨天消息 3",
        "昨天消息 4",
        "今天用户消息",
    ]
    assert "【会话延续摘要】" in llm_input["system_prompt"]
    assert llm_input["metadata"]["carryover_summary_included"] is True
    assert llm_input["metadata"]["carryover_summary_suppressed_by_history"] is False


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
