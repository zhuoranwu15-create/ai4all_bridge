"""nearline 日报相关聚焦测试：

1. H4 质量检查（proactive_reply_fk）对已清空账号的孤儿归因豁免，对活跃账号仍硬失败。
2. 飞书精简摘要渲染（纯函数）及平台发送模块接线。

注：nearline 与主 app 解耦，用临时 sqlite 文件构造最小 facts/source 库，
不依赖标准库 data/ai4all.sqlite3。
"""

import sqlite3
import json
from datetime import datetime

from app.config import settings
from app.platform.observability import alerting as platform_alerting
from nearline import alerting as nearline_alerting
from nearline import run_daily as nearline_run_daily
from nearline.analytics import quality
from nearline.analytics.facts_db import init_facts
from nearline.analytics.metrics import daily_users
from nearline.analytics.warehouse import dim_account, fct_message, fct_proactive
from nearline.reporting import formatter
from nearline.reporting.scope import resolve_scope


def _result(results, name):
    return next(r for r in results if r.name == name)


def _make_source_db(path, *, alive_account_messages):
    """构造最小源库：messages + daily_usage。

    alive_account_messages: dict[account_id -> [message_id, ...]]，列出仍存活的入站消息。
    被清空账号不出现在此 dict，即源库无其任何消息。
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, account_id TEXT, direction TEXT,
            role TEXT, created_at TEXT
        );
        CREATE TABLE daily_usage(date TEXT, message_count INTEGER);
        """
    )
    for aid, ids in alive_account_messages.items():
        for mid in ids:
            conn.execute(
                "INSERT INTO messages(id, account_id, direction, role, created_at) "
                "VALUES (?, ?, 'inbound', 'user', '2026-06-15 10:00:00')",
                (mid, aid),
            )
    conn.commit()
    conn.close()


def _seed_facts(path, *, proactive_rows):
    """构造最小 facts：dim_account + fct_proactive_message。

    proactive_rows: list[(id, account_id, reply_message_id)]，均视为已发送+已回复。
    """
    init_facts(str(path))
    conn = sqlite3.connect(str(path))
    accounts = {aid for _, aid, _ in proactive_rows}
    for aid in accounts:
        conn.execute(
            "INSERT INTO dim_account(account_id, is_debug) VALUES (?, 0)", (aid,)
        )
    for pid, aid, reply_mid in proactive_rows:
        conn.execute(
            "INSERT INTO fct_proactive_message("
            "id, account_id, status, sent_at, sent_date, replied, "
            "reply_message_id, reply_window_hours, resolution_status) "
            "VALUES (?, ?, 'sent', '2026-06-15 08:00:00', '2026-06-15', 1, ?, 24, 'resolved')",
            (pid, aid, reply_mid),
        )
    conn.commit()
    conn.close()


def _make_source_with_content(path, rows):
    """构造带 content 的最小源库（供 S3 重投监测测试）。rows: [(id, account_id, content, created_at)]。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, account_id TEXT, direction TEXT,
            role TEXT, content TEXT, created_at TEXT
        );
        CREATE TABLE daily_usage(date TEXT, message_count INTEGER);
        """
    )
    for mid, aid, content, ts in rows:
        conn.execute(
            "INSERT INTO messages(id, account_id, direction, role, content, created_at) "
            "VALUES (?, ?, 'inbound', 'user', ?, ?)",
            (mid, aid, content, ts),
        )
    conn.commit()
    conn.close()


def test_s3_flags_sub2s_same_content_redelivery(tmp_path):
    """同账号同文 1s 内再次入站 → S3 软检查标记（n≥1，passed=False）。"""
    facts = tmp_path / "facts.sqlite3"
    source = tmp_path / "source.sqlite3"
    init_facts(str(facts))
    _make_source_with_content(source, [
        (1, "aid_x", "在吗", "2026-06-15 10:00:00"),
        (2, "aid_x", "在吗", "2026-06-15 10:00:01"),  # 1s 后同文 → 疑似重投
    ])
    results = quality.run_checks(
        "2026-06-15", source_db_override=str(source), facts_db_override=str(facts)
    )
    r = _result(results, "inbound_redelivery_suspect")
    assert not r.passed
    assert r.detail.startswith("1 ")


def test_s3_ignores_same_content_far_apart(tmp_path):
    """同文但间隔 5 分钟（用户正常重发）→ S3 不标记。"""
    facts = tmp_path / "facts.sqlite3"
    source = tmp_path / "source.sqlite3"
    init_facts(str(facts))
    _make_source_with_content(source, [
        (1, "aid_x", "在吗", "2026-06-15 10:00:00"),
        (2, "aid_x", "在吗", "2026-06-15 10:05:00"),  # 5min 后 → 正常重发，不算重投
    ])
    results = quality.run_checks(
        "2026-06-15", source_db_override=str(source), facts_db_override=str(facts)
    )
    assert _result(results, "inbound_redelivery_suspect").passed


def test_h4_exempts_wiped_account_orphan(tmp_path):
    """孤儿 reply_message_id 属已清空账号（源库无任何消息）→ H4 豁免，硬检查通过。"""
    facts = tmp_path / "facts.sqlite3"
    source = tmp_path / "source.sqlite3"
    # 账号 wiped：归因到的 message 999 已不在源库，且该账号源库无任何消息。
    _seed_facts(facts, proactive_rows=[(1, "aid_wiped", 999)])
    _make_source_db(source, alive_account_messages={})  # 无任何消息

    results = quality.run_checks(
        "2026-06-15", source_db_override=str(source), facts_db_override=str(facts)
    )
    r = _result(results, "proactive_reply_fk")
    assert r.passed, r.detail
    assert "已豁免" in r.detail


def test_h4_hard_fails_for_alive_account_orphan(tmp_path):
    """孤儿 reply_message_id 属仍存活账号（源库还有其它消息）→ 真实外键 bug，硬失败。"""
    facts = tmp_path / "facts.sqlite3"
    source = tmp_path / "source.sqlite3"
    # 账号存活：源库还有它的消息 500，但归因到的 999 缺失 → 真实 bug。
    _seed_facts(facts, proactive_rows=[(1, "aid_alive", 999)])
    _make_source_db(source, alive_account_messages={"aid_alive": [500]})

    results = quality.run_checks(
        "2026-06-15", source_db_override=str(source), facts_db_override=str(facts)
    )
    r = _result(results, "proactive_reply_fk")
    assert not r.passed
    assert "活跃账号" in r.detail


def test_h4_passes_when_reply_present(tmp_path):
    """归因到的 message 在源库存在 → H4 正常通过。"""
    facts = tmp_path / "facts.sqlite3"
    source = tmp_path / "source.sqlite3"
    _seed_facts(facts, proactive_rows=[(1, "aid_ok", 500)])
    _make_source_db(source, alive_account_messages={"aid_ok": [500]})

    results = quality.run_checks(
        "2026-06-15", source_db_override=str(source), facts_db_override=str(facts)
    )
    assert _result(results, "proactive_reply_fk").passed


def _sections():
    return {
        "users": {
            "date": "2026-06-15", "new_users": 7, "dau": 14, "inbound_messages": 102,
            "d1_status": "observe_only", "d1_cohort_size": 4, "d1_retained": 2,
            "d1_retention_rate": 0.5,
        },
        "proactive": {
            "total_sent": 19, "covered_accounts": 8, "blocked_count": 1, "failed_count": 3,
            "reply_rate_overall": 0.5, "replied_total": 5, "resolved_sent": 10,
            "reply_latency_p50_sec": 1200, "by_category": {}, "reply_window_hours": 24,
        },
        "dreaming": {
            "runs_total": 8, "runs_succeeded": 8, "runs_partial": 0, "runs_failed": 0,
            "items_generated": 24, "items_applied": 20, "items_applied_rate": 0.83,
            "items_skipped": 4, "items_by_skip_reason": {}, "avg_duration_sec": 8,
            "tokens_input": None, "tokens_output": None,
            "accounts_covered": 6, "scheduler_status": "ok",
            "scheduler_last_success_at": "2026-06-15 04:01:00",
        },
        "onboarding": {
            "cnt_complete": 5, "cnt_pending": 11, "cnt_step1_sent": 0,
            "cnt_step2_sent": 0, "cnt_step3_sent": 0, "cnt_timed_out": 0,
            "completion_rate": 1.0, "cohort_registered": 7, "cohort_completed": 3,
        },
    }


def test_feishu_summary_renders_core_numbers():
    text = formatter.render_feishu_summary(_sections())
    assert "AI4ALL 每日运营报告 — 2026-06-15" in text
    assert "DAU 14" in text
    assert "发送 19" in text
    assert "发送失败 3" in text
    assert "运行 8" in text
    assert "complete 5" in text
    # 飞书纯文本：不应混入 Markdown 标题井号段
    assert "##" not in text


def test_feishu_summary_includes_quality_notes():
    text = formatter.render_feishu_summary(_sections(), quality_notes=["dau_reconciliation: x"])
    assert "数据质量提示" in text


def test_scoped_users_deduplicates_humans_and_isolates_channel(tmp_path):
    """同一真人的多个 resident 只算 1 DAU，Native 入站不进入微信版。"""
    facts_path = tmp_path / "facts.sqlite3"
    init_facts(str(facts_path))
    conn = sqlite3.connect(str(facts_path))
    conn.executemany(
        "INSERT INTO dim_account("
        "account_id, app_id, channel, platform_user_id, platform_user_registered_date, "
        "registered_date, is_debug) VALUES (?, 'zhaoxi', ?, ?, ?, ?, 0)",
        [
            ("aid_wx_1", "openclaw-weixin", "uid_old", "2026-06-01", "2026-06-01"),
            ("aid_wx_2", "openclaw-weixin", "uid_old", "2026-06-01", "2026-06-15"),
            ("aid_native", "native", "uid_old", "2026-06-01", "2026-06-15"),
            ("aid_new", "openclaw-weixin", "uid_new", "2026-06-15", "2026-06-15"),
        ],
    )
    conn.executemany(
        "INSERT INTO fct_message("
        "message_pk, account_id, channel, direction, role, event_date) "
        "VALUES (?, ?, ?, 'inbound', 'user', '2026-06-15')",
        [
            (1, "aid_wx_1", "openclaw-weixin"),
            (2, "aid_wx_1", "openclaw-weixin"),
            (3, "aid_wx_2", "openclaw-weixin"),
            (4, "aid_native", "native"),
        ],
    )
    conn.commit()
    conn.close()

    result = daily_users.compute(
        "2026-06-15",
        today="2026-06-17",
        facts_db_override=str(facts_path),
        scope=resolve_scope("zhaoxi", "openclaw-weixin"),
    )

    assert result["new_users"] == 1
    assert result["dau"] == 1
    assert result["active_accounts"] == 2
    assert result["inbound_messages"] == 3


def test_scoped_feishu_summary_names_product_channel_and_metric_units():
    sections = _sections()
    sections["users"]["active_accounts"] = 16
    text = formatter.render_feishu_summary(
        sections, scope=resolve_scope("zhaoxi", "openclaw-weixin")
    )

    assert text.startswith("朝夕相伴（微信渠道） 每日运营报告")
    assert "新增真人 7" in text
    assert "真人DAU 14" in text
    assert "活跃AI账号 16" in text
    assert "Dreaming 按 AI 账号归属渠道" in text


def test_report_scope_rejects_unregistered_product_channel():
    try:
        resolve_scope("unknown_product", "native")
    except ValueError as err:
        assert "unregistered report scope" in str(err)
    else:
        raise AssertionError("unknown report scope must fail closed")


def test_native_scope_is_registered_but_not_the_scheduled_default():
    scope = resolve_scope("zhaoxi", "native")
    assert scope.title == "朝夕相伴（App 渠道）"
    assert scope.onboarding_mode == "not_configured"
    sections = _sections()
    sections["users"]["active_accounts"] = 2
    text = formatter.render_daily(sections, scope=scope)
    assert "App Onboarding 指标待事件接入" in text
    assert "存量状态" not in text


def test_run_state_isolated_by_scope_with_default_legacy_alias(tmp_path, monkeypatch):
    legacy = tmp_path / "run_state.json"
    monkeypatch.setattr(nearline_run_daily, "STATE_FILE", legacy)

    nearline_run_daily._save_state(
        {"target_date": "2026-06-15", "channel": "native"},
        "zhaoxi_native",
        legacy_alias=False,
    )
    assert json.loads((tmp_path / "run_state_zhaoxi_native.json").read_text())["channel"] == "native"
    assert not legacy.exists()

    nearline_run_daily._save_state(
        {"target_date": "2026-06-15", "channel": "openclaw-weixin"},
        "zhaoxi_openclaw-weixin",
        legacy_alias=True,
    )
    assert json.loads(legacy.read_text())["channel"] == "openclaw-weixin"


def test_facts_capture_product_owner_and_event_channel(tmp_path):
    """facts 应保存产品、真人 owner 和每条消息的真实渠道。"""
    source_path = tmp_path / "source.sqlite3"
    facts_path = tmp_path / "facts.sqlite3"
    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    source.executescript(
        """
        CREATE TABLE accounts(
            id TEXT PRIMARY KEY, app_id TEXT, channel TEXT, is_debug INTEGER,
            created_at TEXT, onboarding_state TEXT
        );
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, account_id TEXT, session_id INTEGER,
            direction TEXT, role TEXT, message_type TEXT, raw_json TEXT, created_at TEXT
        );
        CREATE TABLE sessions(id INTEGER PRIMARY KEY, business_day TEXT);
        CREATE TABLE account_owner_bindings(
            account_id TEXT, platform_user_id TEXT, status TEXT
        );
        CREATE TABLE platform_users(id TEXT, created_at TEXT);
        CREATE TABLE product_memberships(platform_user_id TEXT, app_id TEXT, created_at TEXT);
        CREATE TABLE universes(id TEXT, owner_platform_user_id TEXT);
        CREATE TABLE universe_residents(universe_id TEXT, runtime_account_id TEXT);
        """
    )
    source.execute(
        "INSERT INTO accounts VALUES "
        "('aid_1', 'zhaoxi', 'openclaw-weixin', 0, '2026-06-10 09:00:00', 'complete')"
    )
    source.execute("INSERT INTO sessions VALUES (1, '2026-06-15')")
    source.execute(
        "INSERT INTO messages VALUES "
        "(1, 'aid_1', 1, 'inbound', 'user', 'text', "
        "'{\"channel\":\"openclaw-weixin\"}', '2026-06-15 10:00:00')"
    )
    source.execute("INSERT INTO account_owner_bindings VALUES ('aid_1', 'uid_1', 'active')")
    source.execute("INSERT INTO platform_users VALUES ('uid_1', '2026-06-10 08:00:00')")
    source.execute("INSERT INTO product_memberships VALUES ('uid_1', 'zhaoxi', '2026-06-11 08:00:00')")
    source.commit()

    init_facts(str(facts_path))
    facts = sqlite3.connect(str(facts_path))
    facts.row_factory = sqlite3.Row
    dim_account.load(source, facts, "2026-06-16T04:00:00")
    fct_message.load(source, facts)
    facts.commit()

    account = facts.execute("SELECT * FROM dim_account WHERE account_id = 'aid_1'").fetchone()
    message = facts.execute("SELECT * FROM fct_message WHERE message_pk = 1").fetchone()
    assert account["app_id"] == "zhaoxi"
    assert account["platform_user_id"] == "uid_1"
    assert account["platform_user_registered_date"] == "2026-06-10"
    # membership 晚于既有产品账号时视为历史回填，产品加入日取更早的账号日。
    assert account["product_member_registered_date"] == "2026-06-10"
    assert message["channel"] == "openclaw-weixin"
    source.close()
    facts.close()


def test_proactive_reply_attribution_does_not_cross_channels(tmp_path):
    """微信主动消息不能把同账号更早的 Native 入站认作回复。"""
    source = sqlite3.connect(str(tmp_path / "source.sqlite3"))
    source.row_factory = sqlite3.Row
    source.executescript(
        """
        CREATE TABLE outbound_messages(
            id INTEGER PRIMARY KEY, account_id TEXT, channel TEXT,
            product_category TEXT, source TEXT, status TEXT, policy_reason TEXT,
            created_at TEXT, scheduled_at TEXT, sent_at TEXT
        );
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY, account_id TEXT, direction TEXT, role TEXT,
            raw_json TEXT, created_at TEXT
        );
        """
    )
    source.execute(
        "INSERT INTO outbound_messages VALUES "
        "(1, 'aid_1', 'openclaw-weixin', 'followup', 'test', 'sent', NULL, "
        "'2026-06-15 08:00:00', NULL, '2026-06-15 08:00:00')"
    )
    source.executemany(
        "INSERT INTO messages VALUES (?, 'aid_1', 'inbound', 'user', ?, ?)",
        [
            (10, '{"channel":"native"}', "2026-06-15 08:05:00"),
            (11, '{"channel":"openclaw-weixin"}', "2026-06-15 08:10:00"),
        ],
    )
    source.commit()

    facts_path = tmp_path / "facts.sqlite3"
    init_facts(str(facts_path))
    facts = sqlite3.connect(str(facts_path))
    facts.row_factory = sqlite3.Row
    fct_proactive.load(source, facts, now=datetime(2026, 6, 16, 9, 0, 0))
    facts.commit()
    row = facts.execute("SELECT * FROM fct_proactive_message WHERE id = 1").fetchone()

    assert row["replied"] == 1
    assert row["reply_message_id"] == 11
    source.close()
    facts.close()


def test_nearline_report_uses_platform_feishu_sender(monkeypatch):
    """日报推送应通过平台 observability 模块发送，避免模块迁移后静默失效。"""
    calls = []
    monkeypatch.setattr(settings, "feishu_website_webhook_url", "https://example.test/hook")
    monkeypatch.setattr(
        platform_alerting,
        "_send_feishu_text",
        lambda url, text, timeout: calls.append((url, text, timeout)),
    )

    assert nearline_alerting.send_report("daily summary") is True
    assert calls == [("https://example.test/hook", "daily summary", 5.0)]


def test_nearline_alert_uses_platform_feishu_sender(monkeypatch):
    """质量告警应通过平台 observability 模块发送并保留 nearline 前缀。"""
    calls = []
    monkeypatch.setattr(settings, "feishu_alert_webhook_url", "https://example.test/hook")
    monkeypatch.setattr(
        platform_alerting,
        "_send_feishu_text",
        lambda url, text, timeout: calls.append((url, text, timeout)),
    )

    assert nearline_alerting.send_alert("quality failed") is True
    assert calls == [
        ("https://example.test/hook", "[ai4all][nearline] quality failed", 3.0)
    ]
