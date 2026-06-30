"""Task 5+6 验收测试：icebreaker.py 核心选取/派发逻辑 + scheduler 接入。

策略：
- pick_icebreaker_script 直接插 DB 数据然后调函数，不 mock DB
- dispatch 层 mock dispatch_proactive_text（只测派发协调逻辑，不走网关）
- scheduler 测试注入 mock step 验证隔离性
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _create_account(account_id: str, fresh_db) -> None:
    from app.db import get_or_create_session
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id=account_id,
            channel="openclaw-weixin",
            sender_id="sender",
            sender_name=None,
            chat_id=f"chat-{account_id}",
            session_key=f"session-{account_id}",
        )


def _insert_script(
    script_id: str,
    script_type: str = "小测试",
    marketing_feel: int = 1,
    freq_tier: str = "common",
    enabled: int = 1,
    text: str = "测试话术",
) -> None:
    from app.db._core import connect
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO icebreaker_scripts
                (id, script_type, text, reply_cost, freq_tier, marketing_feel, enabled)
            VALUES (?, ?, ?, '低', ?, ?, ?)
            """,
            (script_id, script_type, text, freq_tier, marketing_feel, enabled),
        )


def _insert_impression(account_id: str, script_id: str, script_type: str, status: str = "sent") -> int:
    from app.db import create_icebreaker_impression
    return create_icebreaker_impression(
        account_id=account_id,
        script_id=script_id,
        outbound_message_id=None,
        script_type=script_type,
        marketing_feel=1,
        status=status,
    )


# ---------------------------------------------------------------------------
# 1. pick_icebreaker_script — 过滤规则
# ---------------------------------------------------------------------------

def test_pick_filters_disabled(fresh_db):
    """disabled 话术不应被选中。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-disabled", fresh_db)
    _insert_script("破冰D01", enabled=0)
    _insert_script("破冰D02", enabled=1)

    result = pick_icebreaker_script("acc-disabled")
    assert result is not None
    assert result["id"] != "破冰D01"
    assert result["id"] == "破冰D02"


def test_pick_filters_high_marketing_feel(fresh_db):
    """marketing_feel >= 3 的话术不应被选中。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-mktfeel", fresh_db)
    _insert_script("破冰M01", marketing_feel=3)
    _insert_script("破冰M02", marketing_feel=1)

    result = pick_icebreaker_script("acc-mktfeel")
    assert result is not None
    assert result["id"] == "破冰M02"


def test_pick_filters_marketing_feel_2_allowed(fresh_db):
    """marketing_feel=2 的话术应允许被选中。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-mkt2", fresh_db)
    _insert_script("破冰F01", marketing_feel=2)

    result = pick_icebreaker_script("acc-mkt2")
    assert result is not None
    assert result["id"] == "破冰F01"


def test_pick_excludes_recent_sent_scripts(fresh_db):
    """最近 30 天内 status='sent' 的话术不应再被选。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-recent", fresh_db)
    _insert_script("破冰R01")
    _insert_script("破冰R02")
    _insert_impression("acc-recent", "破冰R01", "小测试", status="sent")

    result = pick_icebreaker_script("acc-recent")
    assert result is not None
    assert result["id"] == "破冰R02", "已发 sent 的话术不应再被选"


def test_pick_does_not_exclude_cancelled(fresh_db):
    """status='cancelled' 的 impression 不计入 30 天去重，允许重选。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-cancel", fresh_db)
    _insert_script("破冰C01")
    _insert_impression("acc-cancel", "破冰C01", "小测试", status="cancelled")

    # 只有一条话术，且被 cancelled 了，应该还能被选中
    result = pick_icebreaker_script("acc-cancel")
    assert result is not None
    assert result["id"] == "破冰C01"


def test_pick_returns_none_when_pool_empty(fresh_db):
    """候选池为空时返回 None，不报错。"""
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-empty", fresh_db)
    # 不插任何 script

    result = pick_icebreaker_script("acc-empty")
    assert result is None


def test_pick_avoids_same_type_as_last(fresh_db):
    """上一条 impression 的类型应尽量避免连续。

    插 4 条候选：3 条 A 类型 + 1 条 B 类型；上一条发的是 A 类型。
    多次调用取 top_k 随机，B 类型应有更高概率被选；
    这里只验证算法不会 100% 固定选 A 类型（确定性验证：B 权重更高，必然在 top_k 内）。
    """
    from app.proactive.icebreaker import pick_icebreaker_script

    _create_account("acc-type", fresh_db)
    _insert_script("破冰T01", script_type="安全吐槽", freq_tier="common")
    _insert_script("破冰T02", script_type="安全吐槽", freq_tier="common")
    _insert_script("破冰T03", script_type="安全吐槽", freq_tier="common")
    _insert_script("破冰T04", script_type="小测试", freq_tier="common")

    # 上一条发了安全吐槽
    _insert_impression("acc-type", "破冰T01", "安全吐槽", status="sent")

    # 多次采样，B 类型（小测试）应至少出现一次
    selected_types = set()
    for _ in range(20):
        r = pick_icebreaker_script("acc-type")
        if r:
            selected_types.add(r["script_type"])

    assert "小测试" in selected_types, "同类型惩罚后，非同类型话术应能被选中"


# ---------------------------------------------------------------------------
# 2. dispatch_icebreaker — 单账号派发
# ---------------------------------------------------------------------------

def _make_mock_outbound(status: str = "sent") -> dict:
    # id=None：outbound_message_id 允许 NULL（FOREIGN KEY 仅在非 NULL 时校验）
    return {"id": None, "status": status}


def test_dispatch_icebreaker_writes_impression(fresh_db):
    """正常 dispatch 应写入 icebreaker_impressions.status='sent'。"""
    from app.proactive.icebreaker import dispatch_icebreaker
    from app.db._core import connect

    _create_account("acc-disp", fresh_db)
    _insert_script("破冰W01")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-disp",
        "session_key": "session-acc-disp",
    }
    mock_outbound = _make_mock_outbound("sent")

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text", return_value=mock_outbound):
        result = dispatch_icebreaker("acc-disp")

    assert result["status"] == "sent"
    assert result["script_id"] == "破冰W01"
    assert result["impression_id"] is not None

    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM icebreaker_impressions WHERE id=?",
            (result["impression_id"],),
        ).fetchone()
    assert row["status"] == "sent"


def test_dispatch_icebreaker_cancelled_impression(fresh_db):
    """policy 拦截（outbound.status='cancelled'）时，impression.status='cancelled'。"""
    from app.proactive.icebreaker import dispatch_icebreaker
    from app.db._core import connect

    _create_account("acc-cancelled", fresh_db)
    _insert_script("破冰X01")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-cancelled",
        "session_key": "session-acc-cancelled",
    }
    mock_outbound = _make_mock_outbound("cancelled")

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text", return_value=mock_outbound):
        result = dispatch_icebreaker("acc-cancelled")

    assert result["status"] == "cancelled"

    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM icebreaker_impressions WHERE id=?",
            (result["impression_id"],),
        ).fetchone()
    assert row["status"] == "cancelled"


def test_dispatch_icebreaker_no_route(fresh_db):
    """没有 channel route 时 no_op，不写 impression。"""
    from app.proactive.icebreaker import dispatch_icebreaker
    from app.db._core import connect

    _create_account("acc-noroute", fresh_db)
    _insert_script("破冰N01")

    with patch("app.proactive.icebreaker._select_route", return_value=None):
        result = dispatch_icebreaker("acc-noroute")

    assert result["status"] == "no_op"
    assert result["reason"] == "missing_channel_route"

    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM icebreaker_impressions WHERE account_id='acc-noroute'"
        ).fetchone()[0]
    assert count == 0


def test_dispatch_icebreaker_no_candidate(fresh_db):
    """没有候选话术时 no_op，不写 impression。"""
    from app.proactive.icebreaker import dispatch_icebreaker
    from app.db._core import connect

    _create_account("acc-noscript", fresh_db)
    # 不插任何脚本

    mock_route = {"channel": "openclaw-weixin", "channel_account_id": "bot",
                  "to_user_id": "chat-acc-noscript", "session_key": None}

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route):
        result = dispatch_icebreaker("acc-noscript")

    assert result["status"] == "no_op"
    assert result["reason"] == "no_candidate_scripts"

    with connect() as conn:
        count = conn.execute(
            "SELECT count(*) FROM icebreaker_impressions WHERE account_id='acc-noscript'"
        ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# 3. dispatch_due_icebreakers — 批量派发
# ---------------------------------------------------------------------------

def test_dispatch_due_writes_outbound(fresh_db):
    """dispatch_due_icebreakers 应对 due 账号写 outbound_messages。"""
    from app.proactive.icebreaker import dispatch_due_icebreakers

    _create_account("acc-batch1", fresh_db)
    _insert_script("破冰B01")

    mock_route = {"channel": "openclaw-weixin", "channel_account_id": "bot",
                  "to_user_id": "chat-acc-batch1", "session_key": "session-acc-batch1"}

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text",
               return_value={"id": 1001, "status": "sent"}):
        results = dispatch_due_icebreakers(
            now=datetime(2026, 6, 29, 10, 0, 0),
            limit=10,
        )

    assert any(r["account_id"] == "acc-batch1" for r in results)
    sent = [r for r in results if r["account_id"] == "acc-batch1"]
    assert sent[0]["status"] == "sent"


def test_dispatch_due_skips_already_sent_today(fresh_db):
    """今日已有 sent outbound 的账号不应再被派发。"""
    from app.proactive.icebreaker import dispatch_due_icebreakers
    from app.db import create_outbound_message

    _create_account("acc-skip", fresh_db)
    _insert_script("破冰S01")

    # 今天已有 sent outbound
    with patch("app.db.settings", fresh_db):
        create_outbound_message(
            account_id="acc-skip",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-acc-skip",
            session_key="session-acc-skip",
            source="icebreaker",
            text="已发",
            idempotency_key="idem-skip-today",
            quota_date="2026-06-29",
            status="sent",
            product_category="proactive_icebreaker",
        )

    mock_route = {"channel": "openclaw-weixin", "channel_account_id": "bot",
                  "to_user_id": "chat-acc-skip", "session_key": "session-acc-skip"}

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text",
               return_value={"id": 1002, "status": "sent"}) as mock_dispatch:
        results = dispatch_due_icebreakers(
            now=datetime(2026, 6, 29, 10, 0, 0),
            limit=10,
        )

    # acc-skip 今天已发，不应再触发
    dispatched_accounts = [r["account_id"] for r in results]
    assert "acc-skip" not in dispatched_accounts


# ---------------------------------------------------------------------------
# 4. scheduler run_once — icebreaker step 隔离
# ---------------------------------------------------------------------------

def test_scheduler_run_once_includes_icebreaker():
    """run_once 结果应包含 icebreaker_count / icebreakers 字段。"""
    from app.proactive.scheduler import ProactiveScheduler

    mock_icebreakers = MagicMock(return_value=[{"account_id": "a", "status": "sent"}])

    scheduler = ProactiveScheduler(
        interval_seconds=60,
        batch_size=5,
        dispatch_icebreakers=mock_icebreakers,
    )

    result = asyncio.get_event_loop().run_until_complete(scheduler.run_once())
    assert "icebreaker_count" in result
    assert "icebreakers" in result
    assert result["icebreaker_count"] == 1


def test_scheduler_icebreaker_step_failure_does_not_kill_other_steps():
    """icebreaker step 抛异常时，其他 step 仍正常执行，整体状态为 partial_error。"""
    from app.proactive.scheduler import ProactiveScheduler

    def _raise(*a, **kw):
        raise RuntimeError("icebreaker step exploded")

    # 其他 step 返回正常值
    normal = MagicMock(return_value=[])
    scheduler = ProactiveScheduler(
        interval_seconds=60,
        batch_size=5,
        dispatch_reminders=normal,
        dispatch_commitments=normal,
        dispatch_reactivation=normal,
        dispatch_icebreakers=_raise,
        expire_content_invitations=normal,
        scan_account_checks=normal,
    )

    result = asyncio.get_event_loop().run_until_complete(scheduler.run_once())
    assert result["status"] == "partial_error"
    assert result["errors"] is not None
    assert "icebreakers" in result["errors"]
    # 其他 step 正常完成（count 为 0，无 error）
    assert result["reminder_count"] == 0
    assert result["reactivation_count"] == 0


def test_scheduler_other_step_failure_does_not_kill_icebreaker():
    """其他 step 失败时，icebreaker step 仍正常执行。"""
    from app.proactive.scheduler import ProactiveScheduler

    def _raise(*a, **kw):
        raise RuntimeError("reminders exploded")

    mock_icebreakers = MagicMock(return_value=[{"account_id": "x", "status": "sent"}])
    normal = MagicMock(return_value=[])

    scheduler = ProactiveScheduler(
        interval_seconds=60,
        batch_size=5,
        dispatch_reminders=_raise,
        dispatch_commitments=normal,
        dispatch_reactivation=normal,
        dispatch_icebreakers=mock_icebreakers,
        expire_content_invitations=normal,
        scan_account_checks=normal,
    )

    result = asyncio.get_event_loop().run_until_complete(scheduler.run_once())
    assert result["icebreaker_count"] == 1
    assert result["status"] == "partial_error"
    assert "reminders" in result["errors"]


# ---------------------------------------------------------------------------
# 5. Proactive Selection Trace MVP
# ---------------------------------------------------------------------------

def _get_outbound_metadata(outbound_id) -> dict:
    import json
    from app.db._core import connect
    with connect() as conn:
        row = conn.execute(
            "SELECT metadata_json FROM outbound_messages WHERE id=?",
            (outbound_id,),
        ).fetchone()
    if row and row["metadata_json"]:
        return json.loads(row["metadata_json"])
    return {}


def test_dispatch_icebreaker_trace_written(fresh_db):
    """成功发送时，outbound_messages.metadata_json 应包含 decision_trace selection trace。"""
    from app.proactive.icebreaker import dispatch_icebreaker

    _create_account("acc-trace1", fresh_db)
    _insert_script("破冰TR01", script_type="小测试")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-trace1",
        "session_key": "session-acc-trace1",
    }

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text",
               return_value={"id": None, "status": "sent"}) as mock_dispatch:
        result = dispatch_icebreaker("acc-trace1", trigger_source="scheduler")

    assert result["status"] == "sent"

    # dispatch_proactive_text 被调用时，metadata 参数应包含 decision_trace
    call_kwargs = mock_dispatch.call_args.kwargs
    meta = call_kwargs.get("metadata", {})
    dt = meta.get("decision_trace", {})

    assert dt.get("trace_type") == "proactive_selection_trace"
    assert dt.get("trace_version") == 1
    assert dt["l1_trigger"]["trigger_type"] == "icebreaker"
    assert dt["l1_trigger"]["trigger_source"] == "scheduler"
    assert dt["l3_how"]["script_id"] == "破冰TR01"
    assert dt["l3_how"]["script_type"] == "小测试"
    assert dt["l3_how"]["marketing_feel"] is not None
    assert dt["l3_how"]["reply_cost"] is not None
    assert dt["l3_how"]["freq_tier"] is not None


def test_dispatch_icebreaker_trace_on_cancel(fresh_db):
    """policy 拦截 cancelled 时，metadata 里仍应有 decision_trace.l3_how.script_id。

    即使没发出去，也要知道"选了哪条话术、但被 policy 拦了"。
    """
    from app.proactive.icebreaker import dispatch_icebreaker

    _create_account("acc-trace2", fresh_db)
    _insert_script("破冰TR02", script_type="安全吐槽")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-trace2",
        "session_key": "session-acc-trace2",
    }

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text",
               return_value={"id": None, "status": "cancelled"}) as mock_dispatch:
        result = dispatch_icebreaker("acc-trace2")

    assert result["status"] == "cancelled"

    call_kwargs = mock_dispatch.call_args.kwargs
    meta = call_kwargs.get("metadata", {})
    dt = meta.get("decision_trace", {})

    assert dt.get("trace_type") == "proactive_selection_trace"
    assert dt["l3_how"]["script_id"] == "破冰TR02"


def test_dispatch_icebreaker_trace_l0_context(fresh_db):
    """有上一条 impression 时，l0_context 应记录 last_icebreaker_at / last_category。"""
    from app.proactive.icebreaker import dispatch_icebreaker

    _create_account("acc-trace3", fresh_db)
    _insert_script("破冰TR03", script_type="小测试")
    _insert_script("破冰TR04", script_type="安全吐槽")

    # 先插一条历史 impression（安全吐槽）
    _insert_impression("acc-trace3", "破冰TR03", "小测试", status="sent")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-trace3",
        "session_key": "session-acc-trace3",
    }

    with patch("app.proactive.icebreaker._select_route", return_value=mock_route), \
         patch("app.proactive.icebreaker.dispatch_proactive_text",
               return_value={"id": None, "status": "sent"}) as mock_dispatch:
        dispatch_icebreaker("acc-trace3")

    call_kwargs = mock_dispatch.call_args.kwargs
    meta = call_kwargs.get("metadata", {})
    l0 = meta.get("decision_trace", {}).get("l0_context", {})

    assert l0.get("last_icebreaker_at") is not None, "应记录上一次破冰时间"
    assert l0.get("last_category") == "小测试", "应记录上一条 impression 的 script_type"


def test_dispatch_icebreaker_trace_persisted_to_outbound_metadata(fresh_db):
    """integration：不 mock dispatch_proactive_text，验证 decision_trace 真实写入 outbound_messages.metadata_json。"""
    import json
    from app.proactive.icebreaker import dispatch_icebreaker
    from app.db._core import connect

    _create_account("acc-intg", fresh_db)
    _insert_script("破冰IG01", script_type="小测试")

    mock_route = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot",
        "to_user_id": "chat-acc-intg",
        "session_key": "session-acc-intg",
    }

    # 只 mock _select_route（本地无 channel_bindings），其余全部真实执行
    with patch("app.proactive.icebreaker._select_route", return_value=mock_route):
        result = dispatch_icebreaker("acc-intg", trigger_source="scheduler")

    # policy 可能放行(sent) 也可能拦截(cancelled)，两种都应该写入 outbound
    assert result["status"] in ("sent", "cancelled", "pending"), f"unexpected status: {result}"

    with connect() as conn:
        row = conn.execute(
            """
            SELECT metadata_json FROM outbound_messages
            WHERE account_id = 'acc-intg'
              AND product_category = 'proactive_icebreaker'
            ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()

    assert row is not None, "outbound_messages 应有一条 proactive_icebreaker 记录"  # 断言 1

    assert row["metadata_json"] is not None, "metadata_json 不应为 NULL"            # 断言 2a
    meta = json.loads(row["metadata_json"])                                           # 断言 2b：loads 失败即报错
    assert isinstance(meta, dict), "metadata_json 应为合法 JSON 对象"                # 断言 2c

    dt = meta.get("decision_trace", {})

    assert dt.get("trace_type") == "proactive_selection_trace"                        # 断言 3
    assert dt.get("trace_version") == 1                                               # 断言 4
    assert dt["l1_trigger"]["trigger_type"] == "icebreaker"                           # 断言 5
    assert dt["l1_trigger"]["trigger_source"] == "scheduler"                          # 断言 6
    assert dt["l3_how"]["script_id"], "l3_how.script_id 不应为空"                    # 断言 7（非空即可）
    assert dt["l3_how"]["script_id"] == "破冰IG01"                                   # 断言 7+（等值验证）
