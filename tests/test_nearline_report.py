"""nearline 日报相关聚焦测试：

1. H4 质量检查（proactive_reply_fk）对已清空账号的孤儿归因豁免，对活跃账号仍硬失败。
2. 飞书精简摘要渲染（纯函数）。

注：nearline 与主 app 解耦，用临时 sqlite 文件构造最小 facts/source 库，
不依赖标准库 data/ai4all.sqlite3。
"""

import sqlite3

from nearline.analytics import quality
from nearline.analytics.facts_db import init_facts
from nearline.reporting import formatter


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
