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


def test_daily_scan_rotates_weixin_web_and_app_scopes(fresh_db):
    """§7.1：每日轮转同扫微信、Web、App，按各自 active key 归档且摘要不串线。"""
    from app.session_lifecycle import (
        get_or_create_account_active_session_with_dreaming,
        run_daily_dreaming_scan,
    )
    from app.db import (
        ACCOUNT_ACTIVE_SESSION_KEY,
        APP_ACTIVE_SESSION_KEY,
        WEB_ACTIVE_SESSION_KEY,
        insert_message,
        get_session,
    )

    account_id = "acc-triscope"
    # day1：同一账号分别在微信、Web 与 App scope 各开一条 active session。
    wx = get_or_create_account_active_session_with_dreaming(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c", business_day="2026-05-24",
    )["session"]
    web = get_or_create_account_active_session_with_dreaming(
        account_id=account_id, channel="web", sender_id="s",
        sender_name=None, chat_id=None, business_day="2026-05-24",
        active_session_key=WEB_ACTIVE_SESSION_KEY,
    )["session"]
    app = get_or_create_account_active_session_with_dreaming(
        account_id=account_id, channel="native", sender_id="s",
        sender_name=None, chat_id=None, business_day="2026-05-24",
        active_session_key=APP_ACTIVE_SESSION_KEY,
    )["session"]
    assert wx["session_key"] == ACCOUNT_ACTIVE_SESSION_KEY
    assert web["session_key"] == WEB_ACTIVE_SESSION_KEY
    assert app["session_key"] == APP_ACTIVE_SESSION_KEY
    assert len({wx["id"], web["id"], app["id"]}) == 3
    scoped_content = (
        (wx["id"], "微信侧唯一内容"),
        (web["id"], "Web 侧唯一内容"),
        (app["id"], "App 侧唯一内容"),
    )
    for sid, content in scoped_content:
        insert_message(
            account_id=account_id, session_id=int(sid), message_id=f"m-{sid}",
            reply_to_message_id=None, direction="inbound", role="user",
            message_type="text", content=content,
        )

    # day2 04:05 scheduler 扫描：三个 scope 的到期 active 都应被关闭。
    scan = run_daily_dreaming_scan(now=datetime(2026, 5, 25, 4, 5, 0))
    assert scan["scanned"] >= 3

    closed_wx = get_session(session_id=int(wx["id"]))
    closed_web = get_session(session_id=int(web["id"]))
    closed_app = get_session(session_id=int(app["id"]))
    assert closed_wx["status"] == "closed"
    assert closed_web["status"] == "closed"
    assert closed_app["status"] == "closed"
    # 各自按自身 scope 前缀归档（互不串）。
    assert closed_wx["session_key"] == f"{ACCOUNT_ACTIVE_SESSION_KEY}:{wx['id']}"
    assert closed_web["session_key"] == f"{WEB_ACTIVE_SESSION_KEY}:{web['id']}"
    assert closed_app["session_key"] == f"{APP_ACTIVE_SESSION_KEY}:{app['id']}"
    closed = (closed_wx, closed_web, closed_app)
    contents = tuple(content for _sid, content in scoped_content)
    for index, session in enumerate(closed):
        summary = session["carryover_summary"]
        assert contents[index] in summary
        assert all(other not in summary for other in contents if other != contents[index])


def test_carryover_seed_isolated_by_scope(fresh_db):
    """§7.2 Model B：新 active session 只承接**同 scope** 的上一段 carryover。
    微信关闭的 session carryover 不得被 Web 首开的新 session 继承（反之亦然）。"""
    from app.db import (
        ACCOUNT_ACTIVE_SESSION_KEY,
        WEB_ACTIVE_SESSION_KEY,
        get_latest_closed_carryover_for_account,
        get_or_create_session,
        close_session,
    )

    account_id = "acc-carryover-scope"
    # 造一个微信 scope 的 closed session，带 carryover。
    wx = get_or_create_session(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c", session_key=ACCOUNT_ACTIVE_SESSION_KEY,
        business_day="2026-05-24",
    )["session"]
    close_session(
        session_id=int(wx["id"]), close_reason="daily_dreaming",
        archived_session_key=f"{ACCOUNT_ACTIVE_SESSION_KEY}:{wx['id']}",
        carryover_summary="微信侧的延续摘要",
    )

    # 微信 scope 查询：拿得到微信 carryover。
    assert get_latest_closed_carryover_for_account(
        account_id=account_id, active_session_key=ACCOUNT_ACTIVE_SESSION_KEY
    ) == "微信侧的延续摘要"
    # Web scope 查询：无同 scope closed session → None（不跨渠道继承）。
    assert get_latest_closed_carryover_for_account(
        account_id=account_id, active_session_key=WEB_ACTIVE_SESSION_KEY
    ) is None
    # 不传 scope（现状口径）：仍拿到最近任意 closed（微信单渠道下等价）。
    assert get_latest_closed_carryover_for_account(
        account_id=account_id
    ) == "微信侧的延续摘要"

