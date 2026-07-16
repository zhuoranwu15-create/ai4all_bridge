from datetime import datetime, timedelta
from unittest.mock import patch

from app.time_utils import beijing_naive_now


from tests.factories import create_account as _create_account


from tests.factories import create_route as _create_route


def test_reminder_create_due_claim_and_mark_sent(fresh_db):
    from app.db import (
        claim_due_reminder,
        create_outbound_message,
        create_reminder,
        list_due_reminders,
        mark_reminder_sent,
    )

    with patch("app.db.settings", fresh_db):
        _create_account("acc-reminder")
        reminder = create_reminder(
            reminder_id="rem-1",
            account_id="acc-reminder",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-reminder",
            text="提醒内容",
            due_at="2026-05-22 10:00:00",
            metadata={"kind": "manual"},
        )
        due = list_due_reminders(now="2026-05-22 10:00:00")
        claimed = claim_due_reminder(
            reminder_id="rem-1",
            now="2026-05-22 10:00:00",
        )
        duplicate_claim = claim_due_reminder(
            reminder_id="rem-1",
            now="2026-05-22 10:00:00",
        )
        outbound = create_outbound_message(
            account_id="acc-reminder",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-reminder",
            source="reminder",
            text="提醒内容",
            idempotency_key="reminder-rem-1",
            quota_date="2026-05-22",
        )
        sent = mark_reminder_sent(
            reminder_id="rem-1",
            outbound_message_id=outbound["id"],
        )

    assert reminder["status"] == "pending"
    assert reminder["metadata"]["kind"] == "manual"
    assert [item["id"] for item in due] == ["rem-1"]
    assert claimed["status"] == "sending"
    assert claimed["attempts"] == 1
    assert duplicate_claim is None
    assert sent["status"] == "sent"
    assert sent["outbound_message_id"] == outbound["id"]


def test_dispatch_due_reminders_sends_due_one_shot(fresh_db):
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.obligations.reminders import dispatch_due_reminders

    fresh_db.proactive_outbound_daily_limit = 3
    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:rem-1"},
        ) as mock_send,
    ):
        _create_account("acc-dispatch")
        _create_route("acc-dispatch")
        create_reminder(
            reminder_id="rem-dispatch",
            account_id="acc-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-dispatch",
            text="记得出门",
            due_at="2026-05-22 09:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-dispatch")
        outbound = list_outbound_messages(account_id="acc-dispatch")

    assert results[0]["status"] == "sent"
    assert reminder["status"] == "sent"
    assert reminder["outbound_message_id"] == outbound[0]["id"]
    assert outbound[0]["status"] == "sent"
    assert outbound[0]["source"] == "reminder"
    assert outbound[0]["idempotency_key"] == "reminder-rem-dispatch-20260522095959"
    assert outbound[0]["metadata"]["reminder_id"] == "rem-dispatch"
    mock_send.assert_called_once_with(
        to_user_id="user@im.wechat",
        text="记得出门",
        gateway_timeout_ms=fresh_db.openclaw_gateway_call_timeout_ms,
        account_id="bot-1",
        idempotency_key="reminder-rem-dispatch-20260522095959",
        session_key="session-acc-dispatch",
        channel="openclaw-weixin",
    )


def test_dispatch_due_reminders_skips_not_due(fresh_db):
    from app.db import create_reminder, get_reminder
    from app.proactive.obligations.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.send_weixin_text") as mock_send,
    ):
        _create_account("acc-not-due")
        create_reminder(
            reminder_id="rem-not-due",
            account_id="acc-not-due",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-not-due",
            text="还没到时间",
            due_at="2026-05-22 10:01:00",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-not-due")

    assert results == []
    assert reminder["status"] == "pending"
    mock_send.assert_not_called()


def test_dispatch_due_reminder_bypasses_quiet_hours(fresh_db):
    """user_reminder product_category bypasses quiet hours so the reminder is sent."""
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.obligations.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 23, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:rem-quiet"},
        ) as mock_send,
    ):
        _create_account("acc-quiet-rem")
        _create_route("acc-quiet-rem")
        create_reminder(
            reminder_id="rem-quiet",
            account_id="acc-quiet-rem",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-quiet-rem",
            text="夜间提醒",
            due_at="2026-05-22 22:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-quiet")
        outbound = list_outbound_messages(account_id="acc-quiet-rem")

    assert results[0]["status"] == "sent"
    assert reminder["status"] == "sent"
    assert outbound[0]["status"] == "sent"
    mock_send.assert_called_once()


def test_dispatch_due_reminder_marks_gateway_failure(fresh_db):
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.obligations.reminders import dispatch_due_reminders

    now = datetime(2026, 5, 22, 10, 0)
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            side_effect=RuntimeError("gateway down"),
        ),
    ):
        _create_account("acc-fail-rem")
        _create_route("acc-fail-rem")
        create_reminder(
            reminder_id="rem-fail",
            account_id="acc-fail-rem",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-fail-rem",
            text="会失败的提醒",
            due_at="2026-05-22 09:59:59",
        )
        results = dispatch_due_reminders(now=now)
        reminder = get_reminder(reminder_id="rem-fail")
        outbound = list_outbound_messages(account_id="acc-fail-rem")

    assert results[0]["status"] == "failed"
    assert reminder["status"] == "failed"
    assert reminder["error"] == "gateway down"
    assert reminder["outbound_message_id"] == outbound[0]["id"]
    assert outbound[0]["status"] == "failed"
    assert outbound[0]["attempts"] == 1


def test_reminder_recur_columns_exist(fresh_db):
    from app.db import create_reminder, get_reminder
    with patch("app.db.settings", fresh_db):
        from app.db import get_or_create_session
        get_or_create_session(
            account_id="acc-recur",
            channel="openclaw-weixin",
            sender_id="s",
            sender_name=None,
            chat_id="c",
            session_key="sk-recur",
        )
        r = create_reminder(
            account_id="acc-recur",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-recur",
            text="每周提醒",
            due_at="2026-06-07 09:00:00",
            recur_rule="weekly:5",
        )
        assert r["recur_rule"] == "weekly:5"
        assert r["sent_count"] == 0
        assert r["last_sent_at"] is None


def test_mark_reminder_sent_recurring_clears_previous_stale_error(fresh_db):
    """周期提醒曾因送达窗口跳过而写入 error='proactive_touch_stale'；下一周期正常发送成功后，
    这个旧 error 必须被清空，否则一个已恢复正常的提醒会在 admin/监控视图里被误判为持续异常。"""
    from app.db import create_reminder, get_reminder, mark_reminder_sent, reschedule_reminder_stale_touch

    with patch("app.db.settings", fresh_db):
        _create_account("acc-rem-error-clear")
        create_reminder(
            reminder_id="rem-error-clear",
            account_id="acc-rem-error-clear",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-rem-error-clear",
            text="每天提醒",
            due_at="2026-06-01 09:00:00",
            recur_rule="daily",
        )
        reschedule_reminder_stale_touch(
            reminder_id="rem-error-clear",
            next_due_at="2026-06-02 09:00:00",
            error="proactive_touch_stale",
        )
        skipped = get_reminder(reminder_id="rem-error-clear")
        assert skipped["error"] == "proactive_touch_stale"

        sent = mark_reminder_sent(
            reminder_id="rem-error-clear",
            outbound_message_id=None,
            next_due_at="2026-06-03 09:00:00",
        )

    assert sent["error"] is None


def test_recurring_reminder_resets_after_dispatch(fresh_db):
    from unittest.mock import patch, MagicMock
    from app.db import create_reminder, get_reminder

    with patch("app.db.settings", fresh_db):
        _create_account("acc-recur-dispatch")
        _create_route("acc-recur-dispatch")
        create_reminder(
            reminder_id="rem-recur-1",
            account_id="acc-recur-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@wechat",
            session_key="sk-rd",
            text="每周提醒",
            due_at="2026-05-30 09:00:00",
            recur_rule="weekly:5",  # 每周六
        )

    mock_send = MagicMock(return_value={"id": None, "status": "sent"})
    with patch("app.db.settings", fresh_db), \
         patch("app.proactive.obligations.reminders.dispatch_proactive_text", mock_send):
        from app.proactive.obligations.reminders import dispatch_reminder
        result = dispatch_reminder(
            reminder_id="rem-recur-1",
            now=datetime(2026, 5, 30, 9, 0, 0),
        )

    assert result["status"] == "sent"
    with patch("app.db.settings", fresh_db):
        updated = get_reminder(reminder_id="rem-recur-1")
    # Should have reset to pending with next Saturday's date
    assert updated["status"] == "pending"
    assert updated["sent_count"] == 1
    assert updated["due_at"] == "2026-06-06 09:00:00"


def test_recurring_reminder_remote_pending_advances_not_failed(fresh_db):
    """回归：远程账号（如 aliyun2）循环提醒——dispatch 出站 enqueue 返回 status='pending'，
    过去会被 fixed 分支误判 failed 且不推进周期，导致该每日提醒永久卡死（list_due_reminders 只扫
    pending）。现应视为已交付/在途：推进到下一周期并计入 sent_count。"""
    from unittest.mock import MagicMock
    from app.db import create_reminder, get_reminder

    with patch("app.db.settings", fresh_db):
        _create_account("acc-recur-remote")
        _create_route("acc-recur-remote")
        create_reminder(
            reminder_id="rem-recur-remote",
            account_id="acc-recur-remote",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@wechat",
            session_key="sk-remote",
            text="每天提醒",
            due_at="2026-05-30 09:00:00",
            recur_rule="daily",
        )

    # 远程入队：出站返回 pending（未终结），归属节点稍后 pull 发送。
    mock_send = MagicMock(return_value={"id": None, "status": "pending"})
    with patch("app.db.settings", fresh_db), \
         patch("app.proactive.obligations.reminders.dispatch_proactive_text", mock_send):
        from app.proactive.obligations.reminders import dispatch_reminder
        result = dispatch_reminder(
            reminder_id="rem-recur-remote",
            now=datetime(2026, 5, 30, 9, 0, 0),
        )

    assert result["status"] == "pending"
    with patch("app.db.settings", fresh_db):
        updated = get_reminder(reminder_id="rem-recur-remote")
    # 已推进到下一天并计发送，不再是 failed/卡死。
    assert updated["status"] == "pending"
    assert updated["sent_count"] == 1
    assert updated["due_at"] == "2026-05-31 09:00:00"


def test_recurring_reminder_second_occurrence_actually_sends(fresh_db):
    """回归:周期提醒的第二次触发必须真正发送,而不是被幂等键去重短路。

    走真实出站去重路径(只 mock 网关 send_weixin_text),覆盖
    create_outbound_message 的 INSERT OR IGNORE。修复前第二周期的键与第一
    周期相同,会命中已 sent 的出站行而静默不发;修复后键含当次 due_at。
    """
    from app.db import create_reminder, get_reminder, list_outbound_messages
    from app.proactive.obligations.reminders import dispatch_reminder

    fresh_db.proactive_outbound_daily_limit = 10
    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch(
            "app.proactive.delivery.outbound.send_weixin_text",
            return_value={"messageId": "openclaw-weixin:recur"},
        ) as mock_send,
    ):
        _create_account("acc-recur-twice")
        _create_route("acc-recur-twice")
        create_reminder(
            reminder_id="rem-recur-twice",
            account_id="acc-recur-twice",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-recur-twice",
            text="每周提醒",
            due_at="2026-05-30 09:00:00",
            recur_rule="weekly:5",  # 每周六
        )

        # 第一周期
        first = dispatch_reminder(
            reminder_id="rem-recur-twice",
            now=datetime(2026, 5, 30, 9, 0, 0),
        )
        after_first = get_reminder(reminder_id="rem-recur-twice")
        # 第二周期(已 reschedule 到 6/6)
        second = dispatch_reminder(
            reminder_id="rem-recur-twice",
            now=datetime(2026, 6, 6, 9, 0, 0),
        )
        outbound = list_outbound_messages(account_id="acc-recur-twice")

    assert first["status"] == "sent"
    assert after_first["status"] == "pending"
    assert after_first["due_at"] == "2026-06-06 09:00:00"
    assert second["status"] == "sent"
    # 关键:两次触发都真正调用了网关,且生成两条独立幂等键的出站行。
    assert mock_send.call_count == 2
    keys = {row["idempotency_key"] for row in outbound}
    assert keys == {
        "reminder-rem-recur-twice-20260530090000",
        "reminder-rem-recur-twice-20260606090000",
    }


def test_dispatch_one_shot_reminder_skips_when_touch_stale(fresh_db):
    """一次性提醒：账号超过 24 小时送达窗口时不再触发，直接终态 cancelled，不调用网关。"""
    from app.db import create_reminder, get_reminder
    from app.proactive.obligations.reminders import dispatch_reminder

    with patch("app.db.settings", fresh_db):
        now0 = beijing_naive_now()
        _create_account("acc-rem-stale")
        _create_route("acc-rem-stale")
        create_reminder(
            reminder_id="rem-stale",
            account_id="acc-rem-stale",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-rem-stale",
            text="很久没聊了的提醒",
            due_at=now0.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        )

    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch("app.proactive.delivery.outbound.send_weixin_text") as mock_send,
    ):
        result = dispatch_reminder(
            reminder_id="rem-stale",
            now=now0 + timedelta(hours=25),
        )
        reminder = get_reminder(reminder_id="rem-stale")

    assert result["status"] == "skipped"
    assert result["reason"] == "proactive_touch_stale"
    assert reminder["status"] == "cancelled"
    assert reminder["error"] == "proactive_touch_stale"
    mock_send.assert_not_called()


def test_dispatch_recurring_reminder_skips_and_advances_when_touch_stale(fresh_db):
    """周期提醒：送达窗口过期时跳过本次，不计入 sent_count，但正常推进到下一周期。"""
    from app.db import create_reminder, get_reminder
    from app.proactive.obligations.reminders import dispatch_reminder

    with patch("app.db.settings", fresh_db):
        now0 = beijing_naive_now()
        due_at = now0.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        _create_account("acc-rem-recur-stale")
        _create_route("acc-rem-recur-stale")
        create_reminder(
            reminder_id="rem-recur-stale",
            account_id="acc-rem-recur-stale",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-rem-recur-stale",
            text="每天提醒",
            due_at=due_at,
            recur_rule="daily",
        )

    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch("app.proactive.delivery.outbound.send_weixin_text") as mock_send,
    ):
        result = dispatch_reminder(
            reminder_id="rem-recur-stale",
            now=now0 + timedelta(hours=25),
        )
        reminder = get_reminder(reminder_id="rem-recur-stale")

    expected_next_due = (datetime.strptime(due_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "proactive_touch_stale"
    assert reminder["status"] == "pending"
    assert reminder["due_at"] == expected_next_due
    assert reminder["sent_count"] == 0
    assert reminder["error"] == "proactive_touch_stale"
    mock_send.assert_not_called()


def test_dispatch_recurring_reminder_with_malformed_recur_rule_cancels_instead_of_crashing(fresh_db):
    """recur_rule 数据非法（如经 /debug 补丁写入，绕过了工具侧的 validate_recur_rule）时，
    送达窗口过期分支必须优雅降级为 cancelled，而不是让 compute_next_due_at 抛异常炸掉整批调度。"""
    from app.db import create_reminder, get_reminder
    from app.proactive.obligations.reminders import dispatch_reminder

    with patch("app.db.settings", fresh_db):
        now0 = beijing_naive_now()
        due_at = now0.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        _create_account("acc-rem-bad-rule")
        _create_route("acc-rem-bad-rule")
        create_reminder(
            reminder_id="rem-bad-rule",
            account_id="acc-rem-bad-rule",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-rem-bad-rule",
            text="坏掉的周期规则",
            due_at=due_at,
            recur_rule="not-a-real-rule",
        )

    with (
        patch("app.db.settings", fresh_db),
        patch("app.proactive.delivery.outbound.settings", fresh_db),
        patch("app.proactive.delivery.outbound.send_weixin_text") as mock_send,
    ):
        result = dispatch_reminder(
            reminder_id="rem-bad-rule",
            now=now0 + timedelta(hours=25),
        )
        reminder = get_reminder(reminder_id="rem-bad-rule")

    assert result["status"] == "skipped"
    assert result["reason"] == "proactive_touch_stale"
    assert reminder["status"] == "cancelled"
    assert reminder["error"] == "proactive_touch_stale"
    mock_send.assert_not_called()
