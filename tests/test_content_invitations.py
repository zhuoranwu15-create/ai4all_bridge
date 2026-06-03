import json
from datetime import datetime
from unittest.mock import patch


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


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


def _title_items():
    return [
        {"title": "标题一", "source_name": "source", "url": "https://example.com/1"},
        {"title": "标题二", "source_name": "source", "url": "https://example.com/2"},
        {"title": "标题三", "source_name": "source", "url": "https://example.com/3"},
    ]


def _create_proactive_state(account_id: str) -> None:
    from app.proactive.state import ensure_account_state

    ensure_account_state(
        account_id=account_id,
        enabled=True,
        next_scan_at=datetime(2026, 5, 30, 10, 0),
    )


def _insert_history(account_id: str, text: str) -> None:
    from app.db import insert_message, list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id, limit=1)[0]
    insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"history-{account_id}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=text,
        raw={"source": "test"},
    )


def test_dispatch_due_content_invitation_sends_invitation_only(fresh_db):
    from app.db import create_content_invitation, get_content_invitation, list_outbound_messages
    from app.proactive.content_invitations import dispatch_due_content_invitations

    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    fresh_db.content_invitation_daily_limit = 2
    with patch("app.db.settings", fresh_db), patch("app.proactive.policy.settings", fresh_db):
        _create_account("acc-content-dispatch")
        _create_route("acc-content-dispatch")
        invitation = create_content_invitation(
            account_id="acc-content-dispatch",
            topic="AI",
            invitation_text="我看到几条 AI 相关标题，要不要发你看看？",
            title_items=_title_items(),
            scheduled_at="2026-05-30 10:00:00",
            expires_at="2026-05-31 10:00:00",
        )

    with (
        patch("app.proactive.messaging.settings", fresh_db),
        patch("app.proactive.policy.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:content-1"},
        ) as mock_send,
    ):
        results = dispatch_due_content_invitations(
            now=datetime(2026, 5, 30, 10, 0),
            limit=10,
        )
        updated = get_content_invitation(invitation_id=invitation["id"])
        outbound = list_outbound_messages(account_id="acc-content-dispatch")

    assert results[0]["status"] == "invited"
    assert updated["status"] == "invited"
    assert outbound[0]["source"] == "content_invitation"
    assert outbound[0]["product_category"] == "content_invitation"
    assert outbound[0]["text"] == "我看到几条 AI 相关标题，要不要发你看看？"
    assert "标题一" not in outbound[0]["text"]
    mock_send.assert_called_once()


def test_content_invitation_titles_tool_returns_titles_only(fresh_db):
    from app.db import claim_due_content_invitation, create_content_invitation, mark_content_invitation_invited, get_content_invitation
    from app.tools.content_invitation_handlers import handle_send_content_invitation_titles
    from tests.test_tools_handlers import _make_ctx, _setup_account

    with patch("app.db.settings", fresh_db):
        _setup_account("acc-content-tool")
        invitation = create_content_invitation(
            account_id="acc-content-tool",
            topic="AI",
            invitation_text="要不要看几条 AI 标题？",
            title_items=_title_items(),
            scheduled_at="2026-05-30 10:00:00",
            expires_at="2099-01-01 00:00:00",
        )
        claim_due_content_invitation(
            invitation_id=invitation["id"],
            now="2026-06-02 10:00:00",
        )
        mark_content_invitation_invited(
            invitation_id=invitation["id"],
            outbound_message_id=None,
            invited_at="2026-05-30 10:00:00",
        )

    ctx = _make_ctx("acc-content-tool")
    with patch("app.db.settings", fresh_db):
        result = handle_send_content_invitation_titles(
            {"invitation_id": invitation["id"], "max_titles": 2},
            ctx,
            tool_invocation_id=None,
        )
        updated = get_content_invitation(invitation_id=invitation["id"])

    assert result["status"] == "titles_sent"
    assert result["titles"] == [{"title": "标题一"}, {"title": "标题二"}]
    assert "url" not in result["titles"][0]
    assert updated["status"] == "titles_sent"
    assert updated["trigger_message_id"] == "msg-1"
    assert updated["tool_invocation_id"] is None


def test_content_invitation_feedback_writes_cooldown(fresh_db):
    from app.db import get_content_invitation_preference
    from app.tools.content_invitation_handlers import handle_record_content_invitation_feedback
    from tests.test_tools_handlers import _make_ctx, _setup_account

    with patch("app.db.settings", fresh_db):
        _setup_account("acc-content-feedback")

    ctx = _make_ctx("acc-content-feedback")
    with patch("app.db.settings", fresh_db):
        result = handle_record_content_invitation_feedback(
            {"feedback_type": "less_like_this", "topic": "AI", "note": "少一点"},
            ctx,
            tool_invocation_id=456,
        )
        preference = get_content_invitation_preference(
            account_id="acc-content-feedback",
            topic="AI",
        )

    assert result["status"] == "recorded"
    assert preference["status"] == "cooled_down"
    assert preference["cooldown_until"] is not None
    assert preference["feedback_count"] == 1


def test_turn_injects_content_invitation_response_tools(client, fresh_db):
    from app.db import (
        claim_due_content_invitation,
        create_content_invitation,
        get_or_create_session,
        mark_content_invitation_invited,
        set_account_onboarding_state,
    )

    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="content-turn-session",
            channel="openclaw-weixin",
            sender_id="user-1",
            sender_name=None,
            chat_id="chat-1",
            session_key="content-turn-session",
        )
        set_account_onboarding_state(account_id="content-turn-session", state="complete")
        invitation = create_content_invitation(
            account_id="content-turn-session",
            topic="AI",
            invitation_text="要不要看几条 AI 标题？",
            title_items=_title_items(),
            scheduled_at="2026-05-30 10:00:00",
            expires_at="2099-01-01 00:00:00",
        )
        claim_due_content_invitation(
            invitation_id=invitation["id"],
            now="2026-06-02 10:00:00",
        )
        mark_content_invitation_invited(
            invitation_id=invitation["id"],
            outbound_message_id=None,
            invited_at="2026-05-30 10:00:00",
        )

    with patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)) as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer test-secret"},
            json={
                "session_key": "content-turn-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "好呀",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    tool_names = {t["function"]["name"] for t in mock_llm.call_args.kwargs["tools"]}
    assert "send_content_invitation_titles" in tool_names
    assert "record_content_invitation_feedback" in tool_names
    system_prompt = mock_llm.call_args.kwargs["system_prompt"]
    assert "## 当前内容邀请" in system_prompt
    assert invitation["id"] in system_prompt
    assert "## 当前工具状态" not in system_prompt
    assert "本轮始终提供提醒工具" not in system_prompt
    assert "本轮提供网络搜索工具" not in system_prompt


def test_admin_overview_lists_content_invitations_redacted(client, fresh_db):
    from app.db import create_content_invitation

    with patch("app.db.settings", fresh_db):
        _create_account("acc-content-admin")
        create_content_invitation(
            account_id="acc-content-admin",
            topic="AI",
            invitation_text="要不要看几条 AI 标题？",
            title_items=_title_items(),
            scheduled_at="2026-05-30 10:00:00",
        )

    res = client.get(
        "/admin/accounts/acc-content-admin/proactive-overview",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    item = res.json()["content_invitations"][0]
    assert item["topic"] == "AI"
    assert item["invitation_text_redacted"] is True
    assert item["title_count"] == 3
    assert "title_items" not in item


def test_account_check_content_invitation_generation_creates_candidate_with_tool(fresh_db):
    from app.db import get_content_invitation, list_tool_invocations
    from app.proactive.account_checks import generate_content_invitation_candidate

    fresh_db.llm_api_key = "fake-key"
    fresh_db.proactive_content_invitation_generation_enabled = True
    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"

    _create_account("acc-content-generate")
    _create_route("acc-content-generate")
    _create_proactive_state("acc-content-generate")
    _insert_history("acc-content-generate", "我最近一直在关注 AI 产品和大模型创业。")

    tool_args = {
        "topic": "AI 产品",
        "invitation_text": "我看到几条 AI 产品相关标题，要不要发你看看？",
        "title_items": _title_items(),
        "reason": "用户近期稳定关注 AI 产品",
    }
    responses = [
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-content-1",
                                "type": "function",
                                "function": {
                                    "name": "create_content_invitation_candidate",
                                    "arguments": json.dumps(tool_args, ensure_ascii=False),
                                },
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "已创建内容邀请候选。"},
                }
            ]
        },
    ]

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.llm.settings", fresh_db),
        patch("app.llm._http_chat_with_tools", side_effect=responses) as mock_llm,
    ):
        result = generate_content_invitation_candidate(
            account_id="acc-content-generate",
            now=datetime(2026, 5, 30, 10, 0),
        )
        invitation = get_content_invitation(
            invitation_id=result["content_invitation"]["id"],
        )
        invocations = list_tool_invocations(
            account_id="acc-content-generate",
            tool_name="create_content_invitation_candidate",
        )

    assert result["action"] == "content_invitation_candidate_created"
    assert invitation["status"] == "candidate"
    assert invitation["topic"] == "AI 产品"
    assert invitation["invitation_text"] == "我看到几条 AI 产品相关标题，要不要发你看看？"
    assert len(invitation["title_items"]) == 3
    assert invocations[0]["status"] == "succeeded"
    assert mock_llm.call_count == 2
    first_messages = mock_llm.call_args_list[0].args[0]
    assert "隐藏内容邀请候选生成器" in first_messages[0]["content"]
    assert "AI 产品和大模型创业" in first_messages[1]["content"]
    assert "daily_notes" not in first_messages[1]["content"]


def test_account_check_content_invitation_generation_avoids_pending_user_reminder(fresh_db):
    from app.db import create_reminder
    from app.proactive.account_checks import generate_content_invitation_candidate

    fresh_db.llm_api_key = "fake-key"
    fresh_db.proactive_content_invitation_generation_enabled = True
    _create_account("acc-content-avoid")
    _create_route("acc-content-avoid")
    _create_proactive_state("acc-content-avoid")
    _insert_history("acc-content-avoid", "我最近在关注 AI。")
    create_reminder(
        account_id="acc-content-avoid",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-content-avoid",
        text="用户提醒优先",
        due_at="2026-05-30 12:00:00",
    )

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.llm.settings", fresh_db),
        patch("app.llm._http_chat_with_tools") as mock_llm,
    ):
        result = generate_content_invitation_candidate(
            account_id="acc-content-avoid",
            now=datetime(2026, 5, 30, 10, 0),
        )

    assert result["action"] == "no_op"
    assert result["reason"] == "avoidance_window_user_reminder"
    assert result["metadata"]["avoidance_user_reminder_count"] == 1
    mock_llm.assert_not_called()


def test_account_check_content_invitation_generation_can_be_disabled(fresh_db):
    from app.proactive.account_checks import generate_content_invitation_candidate

    fresh_db.proactive_content_invitation_generation_enabled = False
    _create_account("acc-content-disabled")
    _create_route("acc-content-disabled")
    _create_proactive_state("acc-content-disabled")
    _insert_history("acc-content-disabled", "我最近在关注 AI。")

    with patch("app.proactive.account_checks.settings", fresh_db):
        result = generate_content_invitation_candidate(
            account_id="acc-content-disabled",
            now=datetime(2026, 5, 30, 10, 0),
        )

    assert result["action"] == "no_op"
    assert result["reason"] == "content_invitation_generation_disabled"


def test_admin_run_proactive_check_once_displays_generated_content_invitation(client, fresh_db):
    from app.db import get_content_invitation

    fresh_db.llm_api_key = "fake-key"
    fresh_db.proactive_content_invitation_generation_enabled = True
    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"

    _create_account("acc-content-admin-run")
    _create_route("acc-content-admin-run")
    _create_proactive_state("acc-content-admin-run")
    _insert_history("acc-content-admin-run", "最近我在关注 AI 产品。")

    tool_args = {
        "topic": "AI 产品",
        "invitation_text": "我看到几条 AI 产品相关标题，要不要发你看看？",
        "title_items": _title_items(),
    }
    responses = [
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-admin-content",
                                "type": "function",
                                "function": {
                                    "name": "create_content_invitation_candidate",
                                    "arguments": json.dumps(tool_args, ensure_ascii=False),
                                },
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "已创建。"},
                }
            ]
        },
    ]

    with (
        patch("app.proactive.account_checks.settings", fresh_db),
        patch("app.llm.settings", fresh_db),
        patch("app.llm._http_chat_with_tools", side_effect=responses),
    ):
        res = client.post(
            "/admin/accounts/acc-content-admin-run/proactive-check/run-once",
            headers=ADMIN_HEADERS,
        )

    assert res.status_code == 200
    body = res.json()
    assert body["display"]["content_invitation_generated"] is True
    invitation = body["display"]["content_invitation"]
    assert invitation["topic"] == "AI 产品"
    assert invitation["invitation_text"] == "我看到几条 AI 产品相关标题，要不要发你看看？"
    assert invitation["title_items"][0]["title"] == "标题一"
    assert get_content_invitation(invitation_id=invitation["id"])["status"] == "candidate"


def test_admin_run_proactive_check_once_displays_content_invitation_reason(client, fresh_db):
    fresh_db.llm_api_key = "fake-key"
    fresh_db.proactive_content_invitation_generation_enabled = True
    _create_account("acc-content-admin-reason")
    _create_route("acc-content-admin-reason")
    _create_proactive_state("acc-content-admin-reason")

    res = client.post(
        "/admin/accounts/acc-content-admin-reason/proactive-check/run-once",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    body = res.json()
    assert body["display"]["content_invitation_generated"] is False
    assert body["display"]["reason"] == "no_recent_history"
