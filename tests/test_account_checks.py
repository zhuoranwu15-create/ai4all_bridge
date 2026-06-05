from datetime import datetime
from unittest.mock import patch


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def _create_state(account_id: str, metadata=None) -> None:
    from app.proactive.state import ensure_account_state

    ensure_account_state(
        account_id=account_id,
        enabled=True,
        next_scan_at=datetime(2026, 5, 22, 9, 0),
        metadata=metadata or {},
    )


def _insert_history(account_id: str, text: str, message_id: str = "history-1") -> None:
    from app.db import insert_message, list_sessions_for_account

    sessions = list_sessions_for_account(account_id=account_id)
    assert sessions
    insert_message(
        account_id=account_id,
        session_id=int(sessions[0]["id"]),
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=text,
        raw={"source": "test"},
    )


def test_decide_account_check_no_state_no_op(fresh_db):
    from app.proactive.account_checks import decide_account_check_action

    _create_account("acc-hb-no-state")

    decision = decide_account_check_action(
        account_id="acc-hb-no-state",
        now=datetime(2026, 5, 22, 10, 0),
    )

    assert decision["action"] == "no_op"
    assert decision["reason"] == "proactive_state_missing"


def test_decide_account_check_no_candidate_no_op(fresh_db):
    from app.proactive.account_checks import decide_account_check_action

    _create_account("acc-hb-no-candidate")
    _create_route("acc-hb-no-candidate")
    _create_state("acc-hb-no-candidate")

    decision = decide_account_check_action(
        account_id="acc-hb-no-candidate",
        now=datetime(2026, 5, 22, 10, 0),
    )

    assert decision["action"] == "no_op"
    assert decision["reason"] == "no_candidate"


def test_decide_account_check_candidate_returns_send_text_decision(fresh_db):
    from app.proactive.account_checks import decide_account_check_action

    _create_account("acc-hb-send")
    _create_route("acc-hb-send")
    _create_state(
        "acc-hb-send",
        metadata={
            "account_check_candidate": {
                "id": "candidate-1",
                "text": "记得关注一下事情 B。",
                "source": "test",
                "reason": "manual_test",
            }
        },
    )

    with patch("app.proactive.account_checks.settings", fresh_db):
        decision = decide_account_check_action(
            account_id="acc-hb-send",
            now=datetime(2026, 5, 22, 10, 0),
        )

    assert decision["action"] == "send_text"
    assert decision["source"] == "account_check"
    assert decision["text"] == "记得关注一下事情 B。"
    assert decision["idempotency_key"] == "account-check-acc-hb-send-candidate-1-2026-05-22"
    assert decision["route"]["channel"] == "openclaw-weixin"
    assert decision["route"]["channel_account_id"] == "bot-1"
    assert decision["route"]["to_user_id"] == "user@im.wechat"
    assert decision["candidate"]["reason"] == "manual_test"


def test_decide_account_check_candidate_requires_route(fresh_db):
    from app.proactive.account_checks import decide_account_check_action

    _create_account("acc-hb-missing-route")
    _create_state(
        "acc-hb-missing-route",
        metadata={"account_check_candidate_text": "没有路由就不能发。"},
    )

    decision = decide_account_check_action(
        account_id="acc-hb-missing-route",
        now=datetime(2026, 5, 22, 10, 0),
    )

    assert decision["action"] == "no_op"
    assert decision["reason"] == "missing_channel_route"


def test_execute_account_check_respects_quiet_hours(fresh_db):
    from app.proactive.account_checks import decide_account_check_action, execute_account_check_decision

    _create_account("acc-hb-quiet")
    _create_route("acc-hb-quiet")
    _create_state(
        "acc-hb-quiet",
        metadata={"account_check_candidate_text": "夜间不该主动发。"},
    )

    with patch("app.proactive.account_checks.settings", fresh_db):
        now = datetime(2026, 5, 22, 23, 0)
        decision = decide_account_check_action(
            account_id="acc-hb-quiet",
            now=now,
        )
        execution = execute_account_check_decision(decision=decision, now=now)

    assert decision["action"] == "send_text"
    assert execution["status"] == "cancelled"
    assert execution["reason"] == "quiet_hours"


def test_execute_account_check_respects_companion_daily_limit(fresh_db):
    from app.db import create_outbound_message
    from app.proactive.account_checks import decide_account_check_action, execute_account_check_decision

    fresh_db.companion_followup_daily_limit = 1
    _create_account("acc-hb-limit")
    _create_route("acc-hb-limit")
    _create_state(
        "acc-hb-limit",
        metadata={"account_check_candidate_text": "超过额度不该主动发。"},
    )
    create_outbound_message(
        account_id="acc-hb-limit",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-hb-limit",
        source="account_check",
        text="已占用额度",
        idempotency_key="hb-limit-used",
        quota_date="2026-05-22",
        status="sent",
        product_category="companion_followup",
    )

    with patch("app.proactive.account_checks.settings", fresh_db):
        now = datetime(2026, 5, 22, 10, 0)
        decision = decide_account_check_action(
            account_id="acc-hb-limit",
            now=now,
        )
        execution = execute_account_check_decision(decision=decision, now=now)

    assert decision["action"] == "send_text"
    assert execution["status"] == "cancelled"
    assert execution["reason"] == "daily_limit_exceeded"
    assert execution["outbound_message"]["metadata"]["daily_count"] == 1


def test_execute_account_check_decision_sends_via_outbound_ledger(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.account_checks import (
        decide_account_check_action,
        execute_account_check_decision,
    )

    fresh_db.companion_followup_daily_limit = 3
    _create_account("acc-hb-execute")
    _create_route("acc-hb-execute")
    _create_state(
        "acc-hb-execute",
        metadata={
            "account_check_candidate": {
                "id": "candidate-exec",
                "text": "执行一次主动检查。",
                "source": "test",
            }
        },
    )

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:account-check-1"},
        ) as mock_send,
    ):
        now = datetime(2026, 5, 22, 10, 0)
        decision = decide_account_check_action(account_id="acc-hb-execute", now=now)
        execution = execute_account_check_decision(decision=decision, now=now)
        outbound = list_outbound_messages(account_id="acc-hb-execute")

    assert execution["status"] == "sent"
    assert execution["outbound_message"]["status"] == "sent"
    assert execution["outbound_message"]["source"] == "account_check"
    assert execution["outbound_message"]["product_category"] == "companion_followup"
    assert execution["outbound_message"]["metadata"]["account_check_candidate"]["id"] == "candidate-exec"
    assert outbound[0]["idempotency_key"] == "account-check-acc-hb-execute-candidate-exec-2026-05-22"
    mock_send.assert_called_once_with(
        to_user_id="user@im.wechat",
        text="执行一次主动检查。",
        gateway_timeout_ms=fresh_db.openclaw_gateway_call_timeout_ms,
        account_id="bot-1",
        idempotency_key="account-check-acc-hb-execute-candidate-exec-2026-05-22",
        session_key="session-acc-hb-execute",
        channel="openclaw-weixin",
    )


def test_execute_account_check_decision_skips_no_op(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.account_checks import (
        decide_account_check_action,
        execute_account_check_decision,
    )

    _create_account("acc-hb-execute-noop")
    _create_route("acc-hb-execute-noop")
    _create_state("acc-hb-execute-noop")

    decision = decide_account_check_action(
        account_id="acc-hb-execute-noop",
        now=datetime(2026, 5, 22, 10, 0),
    )
    execution = execute_account_check_decision(
        decision=decision,
        now=datetime(2026, 5, 22, 10, 0),
    )

    assert execution["status"] == "skipped"
    assert execution["reason"] == "no_candidate"
    assert list_outbound_messages(account_id="acc-hb-execute-noop") == []


def test_generate_account_check_candidate_draft_writes_draft_without_enabling_send(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.account_checks import (
        decide_account_check_action,
        generate_account_check_candidate_draft,
    )
    from app.proactive.state import get_account_state

    fresh_db.llm_api_key = "fake-key"
    _create_account("acc-hb-draft")
    _create_route("acc-hb-draft")
    _create_state("acc-hb-draft")
    _insert_history("acc-hb-draft", "昨天我让你提醒我今天检查事情 A，明天可能还要看一下后续 B。")

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch(
            "app.proactive.account_checks.generate_completion",
            return_value=(
                '{"should_send": true, "text": "记得关注一下事情 B。", '
                '"reason": "用户提到后续 B", "confidence": 0.92}'
            ),
        ) as mock_generate,
    ):
        result = generate_account_check_candidate_draft(
            account_id="acc-hb-draft",
            now=datetime(2026, 5, 22, 10, 0),
        )
        decision = decide_account_check_action(
            account_id="acc-hb-draft",
            now=datetime(2026, 5, 22, 10, 0),
        )
        state = get_account_state(account_id="acc-hb-draft")

    assert result["action"] == "draft_candidate"
    assert result["candidate"]["text"] == "记得关注一下事情 B。"
    assert state["metadata"]["account_check_candidate_draft"]["text"] == "记得关注一下事情 B。"
    assert "account_check_candidate" not in state["metadata"]
    assert decision["action"] == "no_op"
    assert decision["reason"] == "no_candidate"
    assert list_outbound_messages(account_id="acc-hb-draft") == []
    mock_generate.assert_called_once()
    messages = mock_generate.call_args.args[0]
    assert messages[0]["role"] == "system"
    assert "隐藏账号主动检查候选生成器" in messages[0]["content"]
    assert "daily_notes" not in messages[1]["content"]
    assert "后续 B" in messages[1]["content"]


def test_generate_account_check_candidate_draft_rejects_low_confidence(fresh_db):
    from app.proactive.account_checks import generate_account_check_candidate_draft
    from app.proactive.state import get_account_state

    fresh_db.llm_api_key = "fake-key"
    _create_account("acc-hb-low-confidence")
    _create_route("acc-hb-low-confidence")
    _create_state(
        "acc-hb-low-confidence",
        metadata={"account_check_candidate_draft": {"text": "旧草稿"}},
    )
    _insert_history("acc-hb-low-confidence", "最近只是普通聊天。")

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch(
            "app.proactive.account_checks.generate_completion",
            return_value=(
                '{"should_send": true, "text": "低置信候选", '
                '"reason": "不够确定", "confidence": 0.4}'
            ),
        ),
    ):
        result = generate_account_check_candidate_draft(
            account_id="acc-hb-low-confidence",
            now=datetime(2026, 5, 22, 10, 0),
        )
        state = get_account_state(account_id="acc-hb-low-confidence")

    assert result["action"] == "no_op"
    assert result["reason"] == "llm_no_candidate"
    assert "account_check_candidate_draft" not in state["metadata"]
    assert state["metadata"]["account_check_candidate_draft_generated_at"] == "2026-05-22 10:00:00"


def test_generate_topic_followup_candidate_creates_reactivation_candidate(fresh_db):
    from app.proactive.account_checks import generate_topic_followup_candidate

    fresh_db.llm_api_key = "fake-key"
    _create_account("acc-topic-followup")
    _create_route("acc-topic-followup")
    _create_state("acc-topic-followup")
    _insert_history("acc-topic-followup", "昨天那个相亲对象让我回消息回得很累。")

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch(
            "app.proactive.account_checks.generate_completion",
            return_value=(
                '{"should_send": true, "text": "昨天那个相亲对象后来有再找你吗？", '
                '"topic": "相亲聊天压力", "reason": "用户最近讨论相亲回复压力", '
                '"confidence": 0.91}'
            ),
        ) as mock_generate,
    ):
        result = generate_topic_followup_candidate(
            account_id="acc-topic-followup",
            now=datetime(2026, 6, 5, 10, 0),
        )

    assert result["action"] == "topic_followup_candidate_created"
    candidate = result["reactivation_candidate"]
    assert candidate["type"] == "topic_followup"
    assert candidate["topic"] == "相亲聊天压力"
    assert candidate["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert candidate["source_message_cutoff_id"] > 0
    messages = mock_generate.call_args.args[0]
    assert "隐藏 topic_followup 拉活候选生成器" in messages[0]["content"]
    assert "最近 72 小时" in messages[0]["content"]
    assert "相亲对象" in messages[1]["content"]
    assert "MEMORY.md" not in messages[1]["content"]


def test_generate_topic_followup_candidate_skips_content_topics(fresh_db):
    from app.proactive.account_checks import generate_topic_followup_candidate

    fresh_db.llm_api_key = "fake-key"
    _create_account("acc-topic-skip-content")
    _create_route("acc-topic-skip-content")
    _create_state("acc-topic-skip-content")
    _insert_history("acc-topic-skip-content", "中亚五国是哪几个国家？")

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch(
            "app.proactive.account_checks.generate_completion",
            return_value=(
                '{"should_send": false, "text": "", "topic": "中亚五国", '
                '"reason": "轻知识话题应交给 content_invitation", "confidence": 0.2}'
            ),
        ),
    ):
        result = generate_topic_followup_candidate(
            account_id="acc-topic-skip-content",
            now=datetime(2026, 6, 5, 10, 0),
        )

    assert result["action"] == "no_op"
    assert result["reason"] == "llm_no_topic_followup_candidate"
    assert "content_invitation" in result["metadata"]["reply"]


def test_promote_account_check_candidate_draft_enables_send_decision(fresh_db):
    from app.proactive.account_checks import (
        decide_account_check_action,
        promote_account_check_candidate_draft,
    )
    from app.proactive.state import get_account_state

    _create_account("acc-hb-promote")
    _create_route("acc-hb-promote")
    _create_state(
        "acc-hb-promote",
        metadata={
            "account_check_candidate_draft": {
                "id": "draft-1",
                "text": "提升后可以发送。",
                "source": "account_check_llm_candidate_v1",
                "reason": "测试提升",
                "confidence": 0.93,
            }
        },
    )

    result = promote_account_check_candidate_draft(
        account_id="acc-hb-promote",
        now=datetime(2026, 5, 22, 10, 0),
    )
    state = get_account_state(account_id="acc-hb-promote")
    decision = decide_account_check_action(
        account_id="acc-hb-promote",
        now=datetime(2026, 5, 22, 10, 0),
    )

    assert result["action"] == "promoted_candidate"
    assert "account_check_candidate_draft" not in state["metadata"]
    assert state["metadata"]["account_check_candidate"]["text"] == "提升后可以发送。"
    assert state["metadata"]["account_check_candidate_promoted_at"] == "2026-05-22 10:00:00"
    assert decision["action"] == "send_text"
    assert decision["candidate"]["id"] == "draft-1"


def test_clear_account_check_candidate_draft_removes_draft(fresh_db):
    from app.proactive.account_checks import clear_account_check_candidate_draft
    from app.proactive.state import get_account_state

    _create_account("acc-hb-clear-draft")
    _create_state(
        "acc-hb-clear-draft",
        metadata={"account_check_candidate_draft": {"text": "不要发送"}},
    )

    result = clear_account_check_candidate_draft(
        account_id="acc-hb-clear-draft",
        now=datetime(2026, 5, 22, 10, 0),
    )
    state = get_account_state(account_id="acc-hb-clear-draft")

    assert result["action"] == "cleared_candidate_draft"
    assert result["had_draft"] is True
    assert "account_check_candidate_draft" not in state["metadata"]
    assert state["metadata"]["account_check_candidate_draft_cleared_at"] == "2026-05-22 10:00:00"
