"""主动消息护栏（§8.3）单测：路由能力过滤 + 投递 fail-fast。

验证新增 Web binding 不会让现有微信主动路由静默改道（原则一硬需求）。
"""

from datetime import datetime
from unittest.mock import patch

from app.db import (
    connect,
    create_or_get_platform_user_by_phone,
    get_or_create_session,
    resolve_resident_memory_scope,
    set_universe_onboarding_state,
    upsert_channel_binding,
)
from app.proactive.contract.common import _select_route
from app.proactive.delivery.outbound import dispatch_proactive_text
from tests.factories import make_resident_account


def _ensure_account(account_id: str) -> None:
    # channel_bindings 有指向 accounts 的外键，先建号（经会话创建）。
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"seed-{account_id}",
        business_day="2026-07-13",
    )


def _bind(account_id: str, *, channel: str, chat_id: str, session_key: str) -> None:
    _ensure_account(account_id)
    upsert_channel_binding(
        account_id=account_id,
        channel=channel,
        session_key=session_key,
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id=chat_id,
        raw_identity={"source": "test"},
    )


def test_select_route_prefers_weixin_even_when_web_binding_is_newer(fresh_db):
    account_id = "acc-guard-route"
    # 先挂微信，再挂 web —— web 的 last_seen_at 更新、按 last_seen_at DESC 排在微信前。
    _bind(account_id, channel="openclaw-weixin", chat_id="wx-user@im", session_key="wx")
    _bind(account_id, channel="web", chat_id="web-user", session_key="web:acc-guard-route")

    route = _select_route(account_id)
    assert route is not None
    # 护栏：即便 web binding 更靠前，仍必须选微信（web supports_proactive=False）。
    assert route["channel"] == "openclaw-weixin"
    assert route["to_user_id"] == "wx-user@im"


def test_select_route_returns_none_for_web_only_account(fresh_db):
    account_id = "acc-guard-webonly"
    _bind(account_id, channel="web", chat_id="web-user", session_key="web:acc-guard-webonly")
    # 只有 web binding 的账号：能力过滤后无可投递渠道 → None（不会误投微信网关）。
    assert _select_route(account_id) is None


def test_select_route_pure_weixin_unchanged(fresh_db):
    account_id = "acc-guard-wx"
    _bind(account_id, channel="openclaw-weixin", chat_id="wx-user@im", session_key="wx")
    route = _select_route(account_id)
    assert route is not None
    assert route["channel"] == "openclaw-weixin"
    assert route["to_user_id"] == "wx-user@im"


def test_dispatch_proactive_text_fail_fast_for_web_never_sends(fresh_db):
    # web 渠道被拦在网关之前：send_weixin_text 零调用、不建 outbound 行。
    with patch("app.proactive.delivery.outbound.send_weixin_text") as send_mock, patch(
        "app.proactive.delivery.outbound.should_inline_dispatch_for_account",
        return_value=True,
    ):
        result = dispatch_proactive_text(
            account_id="acc-guard-dispatch",
            channel="web",
            channel_account_id="bot-1",
            to_user_id="web-user",
            session_key="web:acc-guard-dispatch",
            source="commitment",
            text="hi",
        )
    send_mock.assert_not_called()
    assert result["status"] == "cancelled"
    assert result["error"] == "channel_not_proactive"


def test_native_world_route_delivers_to_inbox_without_weixin(fresh_db):
    user_id = create_or_get_platform_user_by_phone(
        phone="19965001001", display_name="App 入箱用户"
    )["id"]
    account_id = make_resident_account(user_id, "App 入箱居民")
    scope = resolve_resident_memory_scope(runtime_account_id=account_id)
    set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    upsert_channel_binding(
        account_id=account_id,
        channel="native",
        session_key="__app_active__",
        channel_account_id=None,
        sender_id=user_id,
        chat_id=None,
        raw_identity={"source": "test"},
    )
    assert _select_route(account_id) is None

    fresh_db.companion_world_app_inbox_enabled = True
    route = _select_route(account_id)
    assert route is not None and route["channel"] == "native"
    with patch("app.proactive.delivery.outbound.send_weixin_text") as send_mock:
        result = dispatch_proactive_text(
            account_id=account_id,
            channel=route["channel"],
            channel_account_id=route.get("channel_account_id"),
            to_user_id=route["to_user_id"],
            session_key=route.get("session_key"),
            source="commitment",
            text="这条只进入 App 收件箱",
            idempotency_key="commitment-app-inbox-1",
            now=datetime(2026, 7, 22, 12, 0, 0),
            product_category="companion_followup",
            metadata={"commitment_id": "app-inbox-1"},
        )
    send_mock.assert_not_called()
    assert result["status"] == "sent"
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM app_notifications WHERE platform_user_id=?",
            (user_id,),
        ).fetchone()
    assert row["delivery_status"] == "visible"
    assert row["idempotency_key"] == (
        "resident-obligation:v1:commitment:app-inbox-1"
    )

    blocked = dispatch_proactive_text(
        account_id=account_id,
        channel="native",
        channel_account_id=None,
        to_user_id=user_id,
        session_key="__app_active__",
        source="account_check",
        text="M3-5 前不能提前入箱",
        now=datetime(2026, 7, 22, 12, 1, 0),
        product_category="companion_followup",
    )
    assert blocked["status"] == "cancelled"
    assert blocked["error"] == "app_inbox_human_proactive_disabled"

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key="wx-world-primary",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="wx-world-user@im",
        raw_identity={"source": "test"},
    )
    assert _select_route(account_id)["channel"] == "openclaw-weixin"


def test_dispatch_proactive_text_weixin_still_reaches_gateway(fresh_db):
    # 微信渠道不受护栏影响：仍走 send_proactive_text → send_weixin_text（行为等价现状）。
    _ensure_account("acc-guard-wx-dispatch")
    with patch("app.proactive.delivery.outbound.send_weixin_text") as send_mock, patch(
        "app.proactive.delivery.outbound.should_inline_dispatch_for_account",
        return_value=True,
    ):
        send_mock.return_value = {"messageId": "m-1"}
        dispatch_proactive_text(
            account_id="acc-guard-wx-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="wx-user@im",
            session_key="wx",
            source="commitment",
            text="hi",
            # 钉死非静默时刻（默认静默 22:00–08:00），避免深夜跑时被 quiet-hours 策略抑制发送
            now=datetime(2026, 7, 13, 14, 0, 0),
        )
    send_mock.assert_called_once()
