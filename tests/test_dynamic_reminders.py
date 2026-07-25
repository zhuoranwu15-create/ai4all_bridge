"""动态提醒（例行简报）：db 层 + 到期履约状态机 + 远程对账。

覆盖设计文档 §6 的关键不变量：
- fixed/dynamic 扫描互不重叠；每账号活跃上限。
- run 的 UNIQUE(reminder_id, scheduled_for) 去重 + 同周期有限重试。
- sent / 远程 enqueued→对账 / touch_stale 跳过 / 履约失败重试 的状态机。
"""
from unittest.mock import patch

import pytest

from tests.factories import create_account

from app.products.zhaoxi.proactive.fulfillment import FulfillmentResult

_OBLIG = "app.products.zhaoxi.proactive.obligations.reminders"


def _mk_dynamic_reminder(reminder_id, account_id, *, due_at, recur_rule="weekly:0,2,4"):
    from app.db import create_reminder

    return create_reminder(
        reminder_id=reminder_id,
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{account_id}",
        text="AI 热点",
        due_at=due_at,
        recur_rule=recur_rule,
        fulfillment="dynamic",
        content_meta={"topic": "AI 热点新闻", "max_items": 5},
    )


# --------------------------------------------------------------------------- #
# db 层
# --------------------------------------------------------------------------- #

def test_forced_first_tool_choice_defaults_to_auto(fresh_db):
    """thinking 模型（deepseek-v4-pro）只接受 tool_choice='auto'，指定函数/required 会 400。
    默认 force_first 为空 → 履约首轮必须下发 'auto'，强制搜索改由提示词 + search_ok 校验保证。"""
    from app.products.zhaoxi.proactive.fulfillment.dynamic_reminder import _forced_first_tool_choice

    tools = [{"function": {"name": "web_search"}}]
    fresh_db.dynamic_reminder_force_first_tool = ""
    with patch("app.products.zhaoxi.proactive.fulfillment.dynamic_reminder.settings", fresh_db):
        assert _forced_first_tool_choice(tools) == "auto"
        # 显式配置某工具时才下发指定函数（供未来非 thinking provider）。
        fresh_db.dynamic_reminder_force_first_tool = "web_search"
        assert _forced_first_tool_choice(tools) == {
            "type": "function", "function": {"name": "web_search"}
        }


def test_list_due_fulfillment_filter_and_count(fresh_db):
    from app.db import (
        create_reminder,
        count_active_dynamic_reminders_for_account,
        list_due_reminders,
    )

    with patch("app.db.settings", fresh_db):
        create_account("acc-a")
        create_reminder(
            reminder_id="rem-fixed", account_id="acc-a", channel="openclaw-weixin",
            channel_account_id="bot-1", to_user_id="u", session_key="session-acc-a",
            text="喝水", due_at="2026-07-15 08:00:00",
        )
        _mk_dynamic_reminder("rem-dyn", "acc-a", due_at="2026-07-15 08:00:00")

        now = "2026-07-15 09:00:00"
        fixed = list_due_reminders(now=now, fulfillment="fixed")
        dynamic = list_due_reminders(now=now, fulfillment="dynamic")
        assert [r["id"] for r in fixed] == ["rem-fixed"]
        assert [r["id"] for r in dynamic] == ["rem-dyn"]
        # 旧行（fulfillment 默认 fixed）不落进 dynamic 扫描。
        assert count_active_dynamic_reminders_for_account(account_id="acc-a") == 1


def test_claim_run_new_reuse_and_exhaust(fresh_db):
    from app.db import (
        claim_reminder_content_run,
        mark_reminder_content_run_failed,
        mark_reminder_content_run_sent,
    )

    with patch("app.db.settings", fresh_db):
        create_account("acc-b")
        _mk_dynamic_reminder("rem-b", "acc-b", due_at="2026-07-15 08:00:00")
        sched = "2026-07-15 08:00:00"

        r1 = claim_reminder_content_run(reminder_id="rem-b", account_id="acc-b",
                                        scheduled_for=sched, max_attempts=2)
        assert r1 is not None and r1["attempts"] == 1
        # 未终结时再次 claim 同周期 → 复用并 attempts+1（重试）。
        mark_reminder_content_run_failed(run_id=r1["id"], error="boom")
        r2 = claim_reminder_content_run(reminder_id="rem-b", account_id="acc-b",
                                        scheduled_for=sched, max_attempts=2)
        assert r2 is not None and r2["id"] == r1["id"] and r2["attempts"] == 2
        # 达到 max_attempts → 返回 None（放弃重试）。
        mark_reminder_content_run_failed(run_id=r2["id"], error="boom2")
        assert claim_reminder_content_run(reminder_id="rem-b", account_id="acc-b",
                                          scheduled_for=sched, max_attempts=2) is None
        # 终态（sent）→ 永不复用。
        mark_reminder_content_run_sent(run_id=r1["id"])
        assert claim_reminder_content_run(reminder_id="rem-b", account_id="acc-b",
                                          scheduled_for=sched, max_attempts=5) is None


def test_content_run_unique_guard_and_meta_merge(fresh_db):
    from app.db import create_reminder_content_run, update_reminder_content_meta, get_reminder

    with patch("app.db.settings", fresh_db):
        create_account("acc-c")
        _mk_dynamic_reminder("rem-c", "acc-c", due_at="2026-07-15 08:00:00")
        first = create_reminder_content_run(reminder_id="rem-c", account_id="acc-c",
                                            scheduled_for="2026-07-15 08:00:00")
        assert first is not None
        dup = create_reminder_content_run(reminder_id="rem-c", account_id="acc-c",
                                          scheduled_for="2026-07-15 08:00:00")
        assert dup is None  # UNIQUE 去重

        update_reminder_content_meta(reminder_id="rem-c",
                                     patch={"last_success_run_at": "2026-07-15 08:00:03"})
        merged = get_reminder(reminder_id="rem-c")
        assert merged["content_meta"]["topic"] == "AI 热点新闻"  # 原字段保留
        assert merged["content_meta"]["last_success_run_at"] == "2026-07-15 08:00:03"


def test_content_runs_account_isolation(fresh_db):
    from app.db import create_reminder_content_run, list_reminder_content_runs_for_account

    with patch("app.db.settings", fresh_db):
        create_account("acc-x")
        create_account("acc-y")
        _mk_dynamic_reminder("rem-x", "acc-x", due_at="2026-07-15 08:00:00")
        create_reminder_content_run(reminder_id="rem-x", account_id="acc-x",
                                    scheduled_for="2026-07-15 08:00:00")
        assert len(list_reminder_content_runs_for_account(account_id="acc-x")) == 1
        assert list_reminder_content_runs_for_account(account_id="acc-y") == []


# --------------------------------------------------------------------------- #
# 到期履约状态机
# --------------------------------------------------------------------------- #

@pytest.fixture
def _dyn_settings(fresh_db):
    fresh_db.dynamic_reminder_enabled = True
    fresh_db.dynamic_reminder_max_retries = 1
    with patch(f"{_OBLIG}.settings", fresh_db):
        yield fresh_db


def _dispatch(reminder_id, *, now="2026-07-17 08:00:00"):
    from datetime import datetime
    from app.products.zhaoxi.proactive.obligations.reminders import dispatch_dynamic_reminder

    return dispatch_dynamic_reminder(
        reminder_id=reminder_id, now=datetime.strptime(now, "%Y-%m-%d %H:%M:%S")
    )


def test_dispatch_sent_advances_period_and_records_meta(_dyn_settings, fresh_db):
    from app.db import get_reminder, list_reminder_content_runs_for_account

    with patch("app.db.settings", fresh_db):
        create_account("acc-1")
        # 周五到期，recur 一三五 → sent 后应推进到下周一。
        _mk_dynamic_reminder("rem-1", "acc-1", due_at="2026-07-17 08:00:00")

        with patch(f"{_OBLIG}.get_account_touch_state", return_value="ok"), \
             patch(f"{_OBLIG}.fulfill_dynamic_reminder",
                   return_value=FulfillmentResult(ok=True, text="本期简报", search_ok=True)), \
             patch(f"{_OBLIG}.dispatch_proactive_text",
                   return_value={"id": 101, "status": "sent"}):
            res = _dispatch("rem-1")

        assert res["status"] == "sent"
        rem = get_reminder(reminder_id="rem-1")
        assert rem["status"] == "pending"
        assert rem["due_at"] == "2026-07-20 08:00:00"  # 下周一
        assert rem["content_meta"]["last_success_run_at"]
        runs = list_reminder_content_runs_for_account(account_id="acc-1")
        assert runs[0]["status"] == "sent" and runs[0]["generated_text"] == "本期简报"


def test_dispatch_remote_enqueued_then_reconcile(_dyn_settings, fresh_db):
    from app.db import (
        get_reminder,
        list_reminder_content_runs_for_account,
        mark_outbound_message_sent,
        create_outbound_message,
    )
    from app.products.zhaoxi.proactive.obligations.reminders import reconcile_enqueued_reminder_content_runs

    with patch("app.db.settings", fresh_db):
        create_account("acc-1")
        _mk_dynamic_reminder("rem-2", "acc-1", due_at="2026-07-17 08:00:00")
        # 建一条真实 outbound(pending)，模拟远程入队。
        ob = create_outbound_message(
            account_id="acc-1", channel="openclaw-weixin", channel_account_id="bot-1",
            to_user_id="u", session_key="session-acc-1", source="reminder",
            text="本期简报", idempotency_key="reminder-rem-2-20260717080000",
            quota_date="2026-07-17", status="pending",
        )

        with patch(f"{_OBLIG}.get_account_touch_state", return_value="ok"), \
             patch(f"{_OBLIG}.fulfill_dynamic_reminder",
                   return_value=FulfillmentResult(ok=True, text="本期简报", search_ok=True)), \
             patch(f"{_OBLIG}.dispatch_proactive_text",
                   return_value={"id": ob["id"], "status": "pending"}):
            res = _dispatch("rem-2")

        assert res["status"] == "pending"
        # 远程入队：run=enqueued（未终结），提醒已推进到下周期（不卡 sending）。
        runs = list_reminder_content_runs_for_account(account_id="acc-1")
        assert runs[0]["status"] == "enqueued"
        assert get_reminder(reminder_id="rem-2")["status"] == "pending"

        # 归属节点发送后 outbound→sent；对账把 run 翻成 sent。
        mark_outbound_message_sent(outbound_message_id=int(ob["id"]), gateway_message_id="gw-1")
        reconciled = reconcile_enqueued_reminder_content_runs()
        assert reconciled and reconciled[0]["status"] == "sent"
        assert list_reminder_content_runs_for_account(account_id="acc-1")[0]["status"] == "sent"


def test_dispatch_touch_stale_skips_without_fulfill(_dyn_settings, fresh_db):
    from app.db import get_reminder, list_reminder_content_runs_for_account

    with patch("app.db.settings", fresh_db):
        create_account("acc-1")
        _mk_dynamic_reminder("rem-3", "acc-1", due_at="2026-07-17 08:00:00")

        fulfil = patch(f"{_OBLIG}.fulfill_dynamic_reminder")
        with patch(f"{_OBLIG}.get_account_touch_state", return_value="stale"), \
             patch(f"{_OBLIG}.dispatch_proactive_text") as disp, fulfil as f:
            res = _dispatch("rem-3")

        assert res["reason"] == "proactive_touch_stale"
        f.assert_not_called()      # 不可达先不搜索（省成本）
        disp.assert_not_called()
        assert list_reminder_content_runs_for_account(account_id="acc-1")[0]["status"] == "skipped"
        # 已推进到下周期，不卡 sending。
        assert get_reminder(reminder_id="rem-3")["status"] == "pending"


def test_dispatch_fulfillment_failure_retries_then_advances(_dyn_settings, fresh_db):
    from datetime import datetime
    from app.db import get_reminder, list_reminder_content_runs_for_account

    with patch("app.db.settings", fresh_db):
        create_account("acc-1")
        _mk_dynamic_reminder("rem-4", "acc-1", due_at="2026-07-17 08:00:00")
        now = datetime(2026, 7, 17, 8, 0, 0)

        with patch(f"{_OBLIG}.get_account_touch_state", return_value="ok"), \
             patch(f"{_OBLIG}.fulfill_dynamic_reminder",
                   return_value=FulfillmentResult(ok=False, error="search_failed")), \
             patch(f"{_OBLIG}.dispatch_proactive_text") as disp:
            # max_retries=1 → 允许 2 次尝试。第一次失败：retry=True，周期未推进。
            r1 = _dispatch("rem-4")
            assert r1["retry"] is True
            assert get_reminder(reminder_id="rem-4")["due_at"] == "2026-07-17 08:00:00"
            # 第二次失败：达上限，retry=False，推进到下周期。
            r2 = _dispatch("rem-4")
            assert r2["retry"] is False
            assert get_reminder(reminder_id="rem-4")["due_at"] == "2026-07-20 08:00:00"
            disp.assert_not_called()  # 搜索失败从不发送编造内容
        runs = list_reminder_content_runs_for_account(account_id="acc-1")
        assert runs[0]["status"] == "failed" and runs[0]["attempts"] == 2


def test_dispatch_disabled_scan_returns_empty(_dyn_settings, fresh_db):
    from app.products.zhaoxi.proactive.obligations.reminders import dispatch_due_dynamic_reminders

    fresh_db.dynamic_reminder_enabled = False
    with patch("app.db.settings", fresh_db):
        create_account("acc-1")
        _mk_dynamic_reminder("rem-5", "acc-1", due_at="2026-07-17 08:00:00")
        assert dispatch_due_dynamic_reminders() == []
