"""Task 3 验收测试：icebreaker DB 层函数。

不依赖 scheduler / icebreaker.py，只测 DB 层行为。
"""
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


def _create_script(script_id: str, script_type: str = "小测试") -> None:
    """插入一条 icebreaker_scripts 记录，满足 impression FK 约束。"""
    from app.db._core import connect

    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO icebreaker_scripts
                (id, script_type, text, reply_cost, freq_tier)
            VALUES (?, ?, '测试话术', '低', 'common')
            """,
            (script_id, script_type),
        )


def _create_outbound(account_id: str, quota_date: str, status: str, product_category: str = "proactive_icebreaker") -> dict:
    from app.db import create_outbound_message

    return create_outbound_message(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot",
        to_user_id=f"user@{account_id}",
        session_key=f"session-{account_id}",
        source="icebreaker",
        text="今天你更像哪种小动物？",
        idempotency_key=f"idem-{account_id}-{quota_date}-{status}",
        quota_date=quota_date,
        status=status,
        product_category=product_category,
    )


# ---------------------------------------------------------------------------
# 1. import 检查
# ---------------------------------------------------------------------------

def test_icebreaker_db_functions_importable(fresh_db):
    from app.db import (
        create_icebreaker_impression,
        get_last_icebreaker_impression,
        list_accounts_due_for_icebreaker,
        list_recent_icebreaker_script_ids,
        update_icebreaker_impression_feedback,
    )
    assert callable(create_icebreaker_impression)
    assert callable(get_last_icebreaker_impression)
    assert callable(list_accounts_due_for_icebreaker)
    assert callable(list_recent_icebreaker_script_ids)
    assert callable(update_icebreaker_impression_feedback)


# ---------------------------------------------------------------------------
# 2. create_icebreaker_impression 能插入并返回 id
# ---------------------------------------------------------------------------

def test_create_icebreaker_impression_returns_id(fresh_db):
    from app.db import create_icebreaker_impression

    with patch("app.db.settings", fresh_db):
        _create_account("acc-imp")
    _create_script("破冰001", "小测试")

    imp_id = create_icebreaker_impression(
        account_id="acc-imp",
        script_id="破冰001",
        outbound_message_id=None,
        script_type="小测试",
        marketing_feel=1,
        status="sent",
    )
    assert isinstance(imp_id, int)
    assert imp_id > 0


def test_create_icebreaker_impression_cancelled_status(fresh_db):
    from app.db import create_icebreaker_impression

    with patch("app.db.settings", fresh_db):
        _create_account("acc-imp-cancel")
    _create_script("破冰002", "安全吐槽")

    imp_id = create_icebreaker_impression(
        account_id="acc-imp-cancel",
        script_id="破冰002",
        outbound_message_id=None,
        script_type="安全吐槽",
        marketing_feel=1,
        status="cancelled",
    )
    assert isinstance(imp_id, int)
    assert imp_id > 0


def test_create_icebreaker_impression_defaults(fresh_db):
    """negative_signal 默认 0，replied 默认 None。"""
    from app.db import create_icebreaker_impression
    from app.db._core import connect

    with patch("app.db.settings", fresh_db):
        _create_account("acc-defaults")
    _create_script("破冰003", "生活观察")

    create_icebreaker_impression(
        account_id="acc-defaults",
        script_id="破冰003",
        outbound_message_id=None,
        script_type="生活观察",
        marketing_feel=None,
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM icebreaker_impressions WHERE account_id='acc-defaults'"
        ).fetchone()
    assert row["negative_signal"] == 0
    assert row["replied"] is None
    assert row["status"] == "sent"


# ---------------------------------------------------------------------------
# 3. list_recent_icebreaker_script_ids
# ---------------------------------------------------------------------------

def test_list_recent_icebreaker_script_ids_only_sent(fresh_db):
    """只返回 status='sent' 的 script_id，cancelled 不算。"""
    from app.db import create_icebreaker_impression, list_recent_icebreaker_script_ids

    with patch("app.db.settings", fresh_db):
        _create_account("acc-recent")
    _create_script("破冰010", "小测试")
    _create_script("破冰011", "安全吐槽")

    create_icebreaker_impression(
        account_id="acc-recent", script_id="破冰010", outbound_message_id=None,
        script_type="小测试", marketing_feel=1, status="sent",
    )
    create_icebreaker_impression(
        account_id="acc-recent", script_id="破冰011", outbound_message_id=None,
        script_type="安全吐槽", marketing_feel=1, status="cancelled",
    )

    ids = list_recent_icebreaker_script_ids(
        account_id="acc-recent",
        since="2000-01-01 00:00:00",
    )
    assert "破冰010" in ids
    assert "破冰011" not in ids, "cancelled 不应出现在去重列表"


def test_list_recent_icebreaker_script_ids_since_filter(fresh_db):
    """since 过滤：since 之前的记录不返回。"""
    from app.db import create_icebreaker_impression, list_recent_icebreaker_script_ids
    from app.db._core import connect

    with patch("app.db.settings", fresh_db):
        _create_account("acc-since")
    _create_script("破冰020", "小测试")

    imp_id = create_icebreaker_impression(
        account_id="acc-since", script_id="破冰020", outbound_message_id=None,
        script_type="小测试", marketing_feel=1, status="sent",
    )
    # 强制把 created_at 改成很久以前
    with connect() as conn:
        conn.execute(
            "UPDATE icebreaker_impressions SET created_at='2020-01-01 00:00:00' WHERE id=?",
            (imp_id,),
        )

    ids = list_recent_icebreaker_script_ids(
        account_id="acc-since",
        since="2025-01-01 00:00:00",
    )
    assert "破冰020" not in ids, "since 之前的记录不应返回"


# ---------------------------------------------------------------------------
# 4. get_last_icebreaker_impression
# ---------------------------------------------------------------------------

def test_get_last_icebreaker_impression_returns_latest(fresh_db):
    from app.db import create_icebreaker_impression, get_last_icebreaker_impression

    with patch("app.db.settings", fresh_db):
        _create_account("acc-last")
    _create_script("破冰030", "假设题")
    _create_script("破冰031", "轻八卦")

    create_icebreaker_impression(
        account_id="acc-last", script_id="破冰030", outbound_message_id=None,
        script_type="假设题", marketing_feel=1, status="sent",
    )
    create_icebreaker_impression(
        account_id="acc-last", script_id="破冰031", outbound_message_id=None,
        script_type="轻八卦", marketing_feel=2, status="sent",
    )

    last = get_last_icebreaker_impression(account_id="acc-last")
    assert last is not None
    assert last["script_id"] == "破冰031"
    assert last["script_type"] == "轻八卦"
    assert last["marketing_feel"] == 2
    assert "status" in last
    assert "created_at" in last


def test_get_last_icebreaker_impression_none_if_empty(fresh_db):
    from app.db import get_last_icebreaker_impression

    with patch("app.db.settings", fresh_db):
        _create_account("acc-empty-imp")

    result = get_last_icebreaker_impression(account_id="acc-empty-imp")
    assert result is None


# ---------------------------------------------------------------------------
# 5. list_accounts_due_for_icebreaker
# ---------------------------------------------------------------------------

def test_list_accounts_due_returns_active_accounts(fresh_db):
    """active 账号今日没有 icebreaker outbound，应出现在列表里。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-due-1")
        _create_account("acc-due-2")

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")
    assert "acc-due-1" in due
    assert "acc-due-2" in due


def test_list_accounts_due_excludes_already_sent(fresh_db):
    """今日已有 status='sent' 的 proactive_icebreaker outbound，不应再出现。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-sent")

    _create_outbound("acc-sent", "2026-06-29", "sent")

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")
    assert "acc-sent" not in due


def test_list_accounts_due_excludes_pending(fresh_db):
    """今日已有 pending 的 proactive_icebreaker outbound，不应再出现。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-pending")

    _create_outbound("acc-pending", "2026-06-29", "pending")

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")
    assert "acc-pending" not in due


def test_list_accounts_due_includes_if_only_cancelled(fresh_db):
    """今日只有 cancelled 的 icebreaker outbound，应允许当天重试。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-cancelled")

    _create_outbound("acc-cancelled", "2026-06-29", "cancelled")

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")
    assert "acc-cancelled" in due, "cancelled 不应阻止当天重试"


def test_list_accounts_due_different_quota_date(fresh_db):
    """昨天有 sent outbound，今天仍可触达。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-yesterday")

    _create_outbound("acc-yesterday", "2026-06-28", "sent")  # 昨天

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")  # 今天
    assert "acc-yesterday" in due


def test_list_accounts_due_other_category_not_blocked(fresh_db):
    """其他 product_category 的 outbound 不影响 icebreaker 触达。"""
    from app.db import list_accounts_due_for_icebreaker

    with patch("app.db.settings", fresh_db):
        _create_account("acc-other-cat")

    _create_outbound("acc-other-cat", "2026-06-29", "sent", product_category="companion_followup")

    due = list_accounts_due_for_icebreaker(quota_date="2026-06-29")
    assert "acc-other-cat" in due


def test_list_accounts_due_node_id_filter(fresh_db):
    """node_id 过滤：只返回归属该节点的账号。"""
    from app.db import list_accounts_due_for_icebreaker
    from app.db._core import connect

    with patch("app.db.settings", fresh_db):
        _create_account("acc-node-a")
        _create_account("acc-node-b")

    # 给 acc-node-a 设 assigned_node_id
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET assigned_node_id='node-1' WHERE id='acc-node-a'"
        )
        conn.execute(
            "UPDATE accounts SET assigned_node_id='node-2' WHERE id='acc-node-b'"
        )

    due_node1 = list_accounts_due_for_icebreaker(quota_date="2026-06-29", node_id="node-1")
    assert "acc-node-a" in due_node1
    assert "acc-node-b" not in due_node1

    due_node2 = list_accounts_due_for_icebreaker(quota_date="2026-06-29", node_id="node-2")
    assert "acc-node-b" in due_node2
    assert "acc-node-a" not in due_node2


# ---------------------------------------------------------------------------
# 6. update_icebreaker_impression_feedback
# ---------------------------------------------------------------------------

def test_update_icebreaker_impression_feedback(fresh_db):
    from app.db import create_icebreaker_impression, update_icebreaker_impression_feedback
    from app.db._core import connect

    with patch("app.db.settings", fresh_db):
        _create_account("acc-feedback")
    _create_script("破冰050", "假设题")

    imp_id = create_icebreaker_impression(
        account_id="acc-feedback", script_id="破冰050", outbound_message_id=None,
        script_type="假设题", marketing_feel=1, status="sent",
    )

    update_icebreaker_impression_feedback(
        impression_id=imp_id,
        replied=1,
        reply_within_hours=2.5,
        continued_conversation=1,
        negative_signal=0,
    )

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM icebreaker_impressions WHERE id=?", (imp_id,)
        ).fetchone()
    assert row["replied"] == 1
    assert abs(row["reply_within_hours"] - 2.5) < 0.01
    assert row["continued_conversation"] == 1
    assert row["negative_signal"] == 0


def test_update_icebreaker_impression_feedback_partial(fresh_db):
    """只更新部分字段，其余字段保持原值。"""
    from app.db import create_icebreaker_impression, update_icebreaker_impression_feedback
    from app.db._core import connect

    with patch("app.db.settings", fresh_db):
        _create_account("acc-partial")
    _create_script("破冰051", "生活观察")

    imp_id = create_icebreaker_impression(
        account_id="acc-partial", script_id="破冰051", outbound_message_id=None,
        script_type="生活观察", marketing_feel=1, status="sent",
    )

    update_icebreaker_impression_feedback(impression_id=imp_id, replied=0)

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM icebreaker_impressions WHERE id=?", (imp_id,)
        ).fetchone()
    assert row["replied"] == 0
    assert row["reply_within_hours"] is None  # 未更新，保持 NULL
    assert row["negative_signal"] == 0        # 未更新，保持默认 0


def test_update_icebreaker_impression_feedback_noop(fresh_db):
    """全部 None 时不执行 UPDATE，不报错。"""
    from app.db import create_icebreaker_impression, update_icebreaker_impression_feedback

    with patch("app.db.settings", fresh_db):
        _create_account("acc-noop")
    _create_script("破冰052", "轻八卦")

    imp_id = create_icebreaker_impression(
        account_id="acc-noop", script_id="破冰052", outbound_message_id=None,
        script_type="轻八卦", marketing_feel=1, status="sent",
    )
    # 不传任何字段，不应报错
    update_icebreaker_impression_feedback(impression_id=imp_id)
