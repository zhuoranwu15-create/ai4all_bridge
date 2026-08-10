"""主动消息护栏（§8.3）单测：路由能力过滤 + 投递 fail-fast。

验证新增 Web binding 不会让现有微信主动路由静默改道（原则一硬需求）。
"""

from datetime import datetime
from unittest.mock import patch

from app.db import get_or_create_session, upsert_channel_binding
from app.products.zhaoxi.proactive.contract.common import _select_route
from app.products.zhaoxi.proactive.delivery.outbound import dispatch_proactive_text


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
    with patch("app.products.zhaoxi.proactive.delivery.outbound.send_weixin_text") as send_mock, patch(
        "app.products.zhaoxi.proactive.delivery.outbound.should_inline_dispatch_for_account",
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


def test_dispatch_proactive_text_weixin_still_reaches_gateway(fresh_db):
    # 微信渠道不受护栏影响：仍走 send_proactive_text → send_weixin_text（行为等价现状）。
    _ensure_account("acc-guard-wx-dispatch")
    with patch("app.products.zhaoxi.proactive.delivery.outbound.send_weixin_text") as send_mock, patch(
        "app.products.zhaoxi.proactive.delivery.outbound.should_inline_dispatch_for_account",
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
