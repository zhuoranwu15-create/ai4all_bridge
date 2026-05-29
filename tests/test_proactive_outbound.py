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


def test_outbound_message_lifecycle_tracks_usage(fresh_db):
    from app.db import (
        claim_pending_outbound_message,
        create_outbound_message,
        get_outbound_daily_usage,
        mark_outbound_message_sent,
    )

    with patch("app.db.settings", fresh_db):
        _create_account("acc-outbound")
        outbound = create_outbound_message(
            account_id="acc-outbound",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-outbound",
            source="reminder",
            text="提醒内容",
            idempotency_key="idem-1",
            quota_date="2026-05-22",
            metadata={"reminder_id": "r1"},
        )
        usage_before = get_outbound_daily_usage(
            account_id="acc-outbound",
            quota_date="2026-05-22",
        )
        claimed = claim_pending_outbound_message(outbound_message_id=outbound["id"])
        sent = mark_outbound_message_sent(
            outbound_message_id=outbound["id"],
            gateway_message_id="gw-1",
        )
        usage_after = get_outbound_daily_usage(
            account_id="acc-outbound",
            quota_date="2026-05-22",
        )

    assert outbound["status"] == "pending"
    assert outbound["attempts"] == 0
    assert outbound["metadata"]["reminder_id"] == "r1"
    assert usage_before == 1
    assert claimed["status"] == "sending"
    assert claimed["attempts"] == 1
    assert sent["status"] == "sent"
    assert sent["gateway_message_id"] == "gw-1"
    assert usage_after == 1


def test_enqueue_proactive_text_applies_daily_limit(fresh_db):
    from app.db import get_outbound_daily_usage
    from app.proactive.messaging import enqueue_proactive_text

    fresh_db.proactive_outbound_daily_limit = 3
    now = datetime(2026, 5, 22, 10, 0)
    with patch("app.db.settings", fresh_db), patch("app.proactive.messaging.settings", fresh_db):
        _create_account("acc-limit")
        rows = [
            enqueue_proactive_text(
                account_id="acc-limit",
                channel="openclaw-weixin",
                channel_account_id="bot-1",
                to_user_id="user@im.wechat",
                session_key="session-acc-limit",
                source="reminder",
                text=f"提醒 {index}",
                idempotency_key=f"limit-{index}",
                now=now,
            )
            for index in range(4)
        ]
        usage = get_outbound_daily_usage(
            account_id="acc-limit",
            quota_date="2026-05-22",
        )

    assert [row["status"] for row in rows] == [
        "pending",
        "pending",
        "pending",
        "cancelled",
    ]
    assert rows[-1]["error"] == "daily_limit_exceeded"
    assert rows[-1]["metadata"]["daily_count"] == 3
    assert usage == 3


def test_enqueue_proactive_text_blocks_quiet_hours_without_consuming_quota(fresh_db):
    from app.db import get_outbound_daily_usage
    from app.proactive.messaging import enqueue_proactive_text

    now = datetime(2026, 5, 22, 23, 0)
    with patch("app.db.settings", fresh_db), patch("app.proactive.messaging.settings", fresh_db):
        _create_account("acc-quiet")
        row = enqueue_proactive_text(
            account_id="acc-quiet",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-quiet",
            source="heartbeat",
            text="晚间消息",
            idempotency_key="quiet-1",
            now=now,
        )
        usage = get_outbound_daily_usage(
            account_id="acc-quiet",
            quota_date="2026-05-22",
        )

    assert row["status"] == "cancelled"
    assert row["error"] == "quiet_hours"
    assert usage == 0


def test_failed_outbound_attempts_count_toward_daily_limit(fresh_db):
    from app.db import get_outbound_daily_usage
    from app.proactive.messaging import send_proactive_text

    fresh_db.proactive_outbound_daily_limit = 1
    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            side_effect=RuntimeError("gateway down"),
        ),
    ):
        _create_account("acc-failed")
        failed = send_proactive_text(
            account_id="acc-failed",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-failed",
            source="reminder",
            text="会失败",
            idempotency_key="failed-1",
            now=now,
        )
        blocked = send_proactive_text(
            account_id="acc-failed",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-failed",
            source="reminder",
            text="第二条",
            idempotency_key="failed-2",
            now=now,
        )
        usage = get_outbound_daily_usage(
            account_id="acc-failed",
            quota_date="2026-05-22",
        )

    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert blocked["status"] == "cancelled"
    assert blocked["error"] == "daily_limit_exceeded"
    assert usage == 1


def test_send_proactive_text_marks_sent_after_gateway_success(fresh_db):
    from app.proactive.messaging import send_proactive_text

    fresh_db.proactive_outbound_daily_limit = 3
    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.messaging.settings", fresh_db),
        patch(
            "app.proactive.messaging.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:msg-1"},
        ) as mock_send,
    ):
        _create_account("acc-send")
        row = send_proactive_text(
            account_id="acc-send",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-send",
            source="reminder",
            text="主动提醒",
            idempotency_key="send-1",
            now=now,
        )

    assert row["status"] == "sent"
    assert row["attempts"] == 1
    assert row["gateway_message_id"] == "openclaw-weixin:msg-1"
    mock_send.assert_called_once_with(
        to_user_id="user@im.wechat",
        text="主动提醒",
        gateway_timeout_ms=fresh_db.openclaw_gateway_call_timeout_ms,
        account_id="bot-1",
        idempotency_key="send-1",
        session_key="session-acc-send",
        channel="openclaw-weixin",
    )


def test_user_reminder_bypasses_quiet_hours(fresh_db):
    from app.proactive.messaging import enqueue_proactive_text
    from unittest.mock import patch

    with patch("app.proactive.messaging.settings", fresh_db):
        with patch("app.db.settings", fresh_db):
            from app.db import get_or_create_session
            get_or_create_session(
                account_id="acc-cat",
                channel="openclaw-weixin",
                sender_id="s",
                sender_name=None,
                chat_id="c",
                session_key="sk-cat",
            )
        # 22:30 is inside quiet hours (22:00–08:00)
        result = enqueue_proactive_text(
            account_id="acc-cat",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-cat",
            source="reminder",
            text="时间到了",
            now=__import__("datetime").datetime(2026, 5, 30, 22, 30),
            product_category="user_reminder",
        )
        assert result["status"] == "pending", f"Expected pending, got {result['status']}: {result.get('error')}"


def test_companion_followup_blocked_by_quiet_hours(fresh_db):
    from app.proactive.messaging import enqueue_proactive_text
    from unittest.mock import patch

    with patch("app.proactive.messaging.settings", fresh_db):
        with patch("app.db.settings", fresh_db):
            from app.db import get_or_create_session
            get_or_create_session(
                account_id="acc-comp",
                channel="openclaw-weixin",
                sender_id="s",
                sender_name=None,
                chat_id="c",
                session_key="sk-comp",
            )
        result = enqueue_proactive_text(
            account_id="acc-comp",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-comp",
            source="commitment",
            text="跟进一下",
            now=__import__("datetime").datetime(2026, 5, 30, 22, 30),
            product_category="companion_followup",
        )
        assert result["status"] == "cancelled"
        assert result["error"] == "quiet_hours"
