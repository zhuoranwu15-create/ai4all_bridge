"""TDAI 记忆功能消息数自然灰度门控：count helper + volume-eligible sticky 缓存。"""
from unittest.mock import patch


def _session(account_id: str, session_key: str) -> dict:
    from app.db import get_or_create_session

    return get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=session_key,
        carryover_summary=None,
    )


def _add(account_id: str, session_id: int, message_id: str, role: str) -> None:
    from app.db import insert_message

    insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound" if role == "user" else "outbound",
        role=role,
        message_type="text",
        content="x",
        raw={},
    )


def test_count_inbound_only_and_account_scoped(fresh_db):
    """只统计 inbound、且严格按 account_id 隔离（出站与他账号不计入）。"""
    from app.db import count_inbound_messages_for_account

    acc = "acc-count"
    other = "acc-other"
    s = _session(acc, "s1")["session"]["id"]
    so = _session(other, "s2")["session"]["id"]
    _add(acc, s, "u1", "user")
    _add(acc, s, "a1", "assistant")   # 出站不计
    _add(acc, s, "u2", "user")
    _add(other, so, "ou1", "user")    # 他账号不计

    assert count_inbound_messages_for_account(account_id=acc) == 2
    assert count_inbound_messages_for_account(account_id=other) == 1
    assert count_inbound_messages_for_account(account_id="acc-none") == 0


def test_volume_eligible_threshold_and_sticky_cache(fresh_db):
    """达到阈值即合格并固化缓存；阈值<=0 关闭该通道。"""
    from app import turn_service

    acc = "acc-vol"
    s = _session(acc, "s1")["session"]["id"]
    for i in range(3):
        _add(acc, s, f"u{i}", "user")

    turn_service._tdai_memory_eligible_accounts.clear()
    fresh_db.tdai_memory_min_messages = 3
    with patch("app.turn_service.settings", fresh_db):
        assert turn_service._tdai_memory_volume_eligible(acc) is True
    # 固化进 sticky 集合
    assert acc in turn_service._tdai_memory_eligible_accounts

    # 阈值 <= 0 → 关闭（即便已在 sticky 集合，也短路返回 False）
    turn_service._tdai_memory_eligible_accounts.clear()
    fresh_db.tdai_memory_min_messages = 0
    with patch("app.turn_service.settings", fresh_db):
        assert turn_service._tdai_memory_volume_eligible(acc) is False


def test_volume_eligible_below_threshold(fresh_db):
    """低于阈值不合格，且不写入 sticky 集合。"""
    from app import turn_service

    acc = "acc-below"
    s = _session(acc, "s1")["session"]["id"]
    _add(acc, s, "u0", "user")

    turn_service._tdai_memory_eligible_accounts.clear()
    fresh_db.tdai_memory_min_messages = 5
    with patch("app.turn_service.settings", fresh_db):
        assert turn_service._tdai_memory_volume_eligible(acc) is False
    assert acc not in turn_service._tdai_memory_eligible_accounts
