from datetime import datetime


def test_business_day_boundary_at_4am():
    from app.session_lifecycle import business_day_for

    assert business_day_for(datetime(2026, 5, 25, 3, 59)) == "2026-05-24"
    assert business_day_for(datetime(2026, 5, 25, 4, 0)) == "2026-05-25"


def test_account_active_session_rotates_when_business_day_changes(fresh_db):
    from app.db import (
        ACCOUNT_ACTIVE_SESSION_KEY,
        get_or_create_account_active_session,
        insert_message,
        list_sessions_for_account,
    )

    first = get_or_create_account_active_session(
        account_id="acc-lifecycle-day",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-24",
    )["session"]
    insert_message(
        account_id="acc-lifecycle-day",
        session_id=int(first["id"]),
        message_id="msg-old",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="昨天的上下文",
    )

    second = get_or_create_account_active_session(
        account_id="acc-lifecycle-day",
        channel="openclaw-weixin",
        sender_id="sender-1",
        sender_name=None,
        chat_id="chat-1",
        business_day="2026-05-25",
    )["session"]

    assert second["id"] != first["id"]
    assert second["session_key"] == ACCOUNT_ACTIVE_SESSION_KEY
    assert second["business_day"] == "2026-05-25"
    assert "昨天的上下文" in second["carryover_summary"]

    sessions = list_sessions_for_account(account_id="acc-lifecycle-day", limit=10)
    closed = next(item for item in sessions if item["id"] == first["id"])
    assert closed["status"] == "closed"
    assert closed["close_reason"] == "daily_dreaming"
    assert closed["session_key"] == f"{ACCOUNT_ACTIVE_SESSION_KEY}:{first['id']}"


def test_rotation_seeds_new_session_rolling_summary_from_carryover(fresh_db):
    """统一编排 P2：dreaming 轮转时 carryover 作为新 session rolling_summary 的 seed，水位线从 0 起。

    LLM 未配置 → run_dreaming 走确定性兜底，carryover 由旧 session 消息生成，非空。
    """
    from app.session_lifecycle import get_or_create_account_active_session_with_dreaming
    from app.db import insert_message, get_session

    account_id = "acc-seed-rolling"
    first = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        business_day="2026-05-24",
    )["session"]
    insert_message(
        account_id=account_id,
        session_id=int(first["id"]),
        message_id="m1",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="今天聊了 AI 陪伴产品和简洁回复偏好",
    )

    second = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        business_day="2026-05-25",
    )["session"]

    assert second["id"] != first["id"]
    carryover = (second.get("carryover_summary") or "").strip()
    assert carryover != ""
    # 返回的 session dict 立即带 seed（供本轮 build_turn_llm_input 使用）。
    assert (second.get("rolling_summary") or "").strip() == carryover
    assert second.get("rolling_summary_upto_id") is not None
    assert int(second["rolling_summary_upto_id"]) == 0
    # DB 落库一致。
    persisted = get_session(session_id=int(second["id"]))
    assert (persisted.get("rolling_summary") or "").strip() == carryover
    assert int(persisted["rolling_summary_upto_id"]) == 0


def test_scheduler_close_then_next_message_seeds_from_last_closed(fresh_db):
    """P1#1：4 点 scheduler 只关闭旧 session（不即时 seed），下一条消息懒创建的新 active
    应从最近已关闭 session 的 carryover 补种 rolling_summary——否则 scheduler 路径丢失前一天延续。"""
    from app.session_lifecycle import (
        get_or_create_account_active_session_with_dreaming,
        run_daily_dreaming_scan,
    )
    from app.db import insert_message, get_session

    account_id = "acc-sched-seed"
    first = get_or_create_account_active_session_with_dreaming(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c", business_day="2026-05-24",
    )["session"]
    insert_message(
        account_id=account_id, session_id=int(first["id"]), message_id="m1",
        reply_to_message_id=None, direction="inbound", role="user",
        message_type="text", content="昨天聊了 AI 陪伴产品和简洁回复偏好",
    )

    # scheduler 于 day2 04:05 扫描：关闭 day1 session（存 carryover），但**不**建新 session。
    scan = run_daily_dreaming_scan(now=datetime(2026, 5, 25, 4, 5, 0))
    assert scan["scanned"] >= 1

    # day2 用户发消息 → 懒创建新 active（同业务日、不走轮转分支）→ 应补种。
    second = get_or_create_account_active_session_with_dreaming(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c", business_day="2026-05-25",
    )["session"]

    assert second["id"] != first["id"]
    seed = (second.get("rolling_summary") or "").strip()
    assert seed != ""                                   # 补种成功（不丢前一天延续）
    assert int(second["rolling_summary_upto_id"]) == 0
    persisted = get_session(session_id=int(second["id"]))
    assert (persisted.get("rolling_summary") or "").strip() == seed


