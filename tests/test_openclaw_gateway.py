import json
import logging
from unittest.mock import patch

import pytest


class _Completed:
    returncode = 0
    stdout = '{"messageId":"msg-1","channel":"openclaw-weixin"}'
    stderr = ""


class _LogoutCompleted:
    returncode = 0
    stdout = "logged out"
    stderr = ""


def _params_from_call(mock_run):
    cmd = mock_run.call_args.args[0]
    return json.loads(cmd[cmd.index("--params") + 1])


def test_send_weixin_text_calls_gateway_send_with_explicit_route(monkeypatch):
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)
    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=_Completed()) as mock_run:
        result = send_weixin_text(
            to_user_id="peer@im.wechat",
            text="hello",
            account_id="bot-account",
            session_key="agent:main:openclaw-weixin:bot-account:direct:peer@im.wechat",
            idempotency_key="idem-1",
            gateway_timeout_ms=1234,
        )

    assert result["messageId"] == "msg-1"
    cmd = mock_run.call_args.args[0]
    assert cmd[:4] == ["openclaw", "gateway", "call", "send"]
    assert cmd[cmd.index("--timeout") + 1] == "1234"
    params = _params_from_call(mock_run)
    assert params == {
        "channel": "openclaw-weixin",
        "to": "peer@im.wechat",
        "message": "hello",
        "accountId": "bot-account",
        "sessionKey": "agent:main:openclaw-weixin:bot-account:direct:peer@im.wechat",
        "idempotencyKey": "idem-1",
    }


def test_send_weixin_text_generates_idempotency_key_when_missing(monkeypatch):
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)
    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=_Completed()) as mock_run:
        send_weixin_text(
            to_user_id="peer@im.wechat",
            text="hello",
            idempotency_key="  ",
            gateway_timeout_ms=1234,
        )

    params = _params_from_call(mock_run)
    assert params["idempotencyKey"].startswith("ai4all-send-")


@pytest.mark.parametrize(
    ("to_user_id", "text", "message"),
    [
        ("", "hello", "to_user_id is required"),
        ("peer@im.wechat", "", "text is required"),
    ],
)
def test_send_weixin_text_rejects_missing_required_fields(to_user_id, text, message):
    from app.platform.gateways.openclaw import send_weixin_text

    with pytest.raises(ValueError, match=message):
        send_weixin_text(
            to_user_id=to_user_id,
            text=text,
            gateway_timeout_ms=1234,
        )


def test_send_weixin_text_raises_rate_limited_on_ret_minus_2(monkeypatch):
    """CLI 退出码 0 但返回体带 ret=-2 → 抛 OpenClawRateLimited（不再静默标 sent）。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawRateLimited, send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class RateLimited:
        returncode = 0
        stdout = '{"ret":-2,"errmsg":"rate limited"}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=RateLimited()):
        with pytest.raises(OpenClawRateLimited) as exc_info:
            send_weixin_text(
                to_user_id="peer@im.wechat",
                text="hello",
                gateway_timeout_ms=1234,
            )
    assert exc_info.value.ret == -2


def test_send_weixin_text_raises_rate_limited_on_errmsg_only(monkeypatch):
    """无业务码、仅 errmsg 含 rate limit 也应识别为限速。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawRateLimited, send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class RateLimited:
        returncode = 0
        stdout = '{"error":"send blocked: Rate_Limited, retry later"}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=RateLimited()):
        with pytest.raises(OpenClawRateLimited):
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)


def test_send_weixin_text_raises_gateway_error_on_nonzero_ret(monkeypatch):
    """非限速的非零业务码 → 抛普通 OpenClawGatewayError（非限速，不退避）。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawGatewayError, OpenClawRateLimited, send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class Failed:
        returncode = 0
        stdout = '{"ret":500,"errmsg":"internal error"}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=Failed()):
        with pytest.raises(OpenClawGatewayError) as exc_info:
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    # 非限速类不应被识别成 RateLimited
    assert not isinstance(exc_info.value, OpenClawRateLimited)


def test_send_weixin_text_success_with_messageid_not_treated_as_error(monkeypatch):
    """有 messageId 即成功，即使返回体里同时带 ret=0 也不误判。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class Ok:
        returncode = 0
        stdout = '{"messageId":"m-ok","ret":0}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=Ok()):
        result = send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    assert result["messageId"] == "m-ok"


def test_send_weixin_text_raises_on_messageid_plus_ret_minus_2(monkeypatch):
    """业务码优先：同时带 messageId 与 ret=-2 也判失败（治假成功）。

    messageId 是本地 clientId、恒非空，不能当送达证据；旧逻辑"见 messageId 即成功"
    会把被 iLink 软拒的消息误标为已发送。现在业务码优先，应抛 OpenClawRateLimited。
    """
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawRateLimited, send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class FalseSuccess:
        returncode = 0
        stdout = '{"messageId":"m-1","channel":"openclaw-weixin","ret":-2}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=FalseSuccess()):
        with pytest.raises(OpenClawRateLimited) as exc_info:
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    assert exc_info.value.ret == -2


def test_send_weixin_text_reads_ret_from_meta_dock(monkeypatch):
    """WS/网关经 meta dock 透传的业务码也应被识别（openclaw-weixin 把 ret 塞在 meta）。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawRateLimited, send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class MetaReject:
        returncode = 0
        stdout = '{"runId":"r-1","messageId":"m-1","channel":"openclaw-weixin","meta":{"ret":-2,"errmsg":"rate limited"}}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=MetaReject()):
        with pytest.raises(OpenClawRateLimited) as exc_info:
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    assert exc_info.value.ret == -2


def test_send_weixin_text_success_with_meta_ret_zero(monkeypatch):
    """meta 里 ret=0（clean accept 变体）不应误判为失败。"""
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import send_weixin_text

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", False)

    class Ok:
        returncode = 0
        stdout = '{"messageId":"m-ok","channel":"openclaw-weixin","meta":{"ret":0}}'
        stderr = ""

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=Ok()):
        result = send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    assert result["messageId"] == "m-ok"


def test_send_weixin_text_uses_ws_when_enabled(monkeypatch):
    from app.platform.gateways import openclaw as openclaw_gateway

    class FakeClient:
        def __init__(self):
            self.calls = []

        def call(self, **kwargs):
            self.calls.append(kwargs)
            return {"messageId": "ws-1"}

    fake = FakeClient()
    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", True)
    monkeypatch.setattr(
        openclaw_gateway.settings, "openclaw_gateway_ws_fallback_to_cli", True
    )
    monkeypatch.setattr(openclaw_gateway, "_persistent_gateway_client", lambda: fake)
    with patch("app.platform.gateways.openclaw.subprocess.run") as mock_run:
        result = openclaw_gateway.send_weixin_text(
            to_user_id="peer@im.wechat",
            text="hello",
            account_id="bot-account",
            session_key="session-1",
            idempotency_key="idem-ws",
            gateway_timeout_ms=1234,
        )

    assert result["messageId"] == "ws-1"
    assert not mock_run.called
    assert fake.calls == [
        {
            "method": "send",
            "params": {
                "channel": "openclaw-weixin",
                "to": "peer@im.wechat",
                "message": "hello",
                "accountId": "bot-account",
                "sessionKey": "session-1",
                "idempotencyKey": "idem-ws",
            },
            "timeout_ms": 1234,
        }
    ]


def test_send_weixin_text_falls_back_to_cli_on_ws_error(monkeypatch, caplog):
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawGatewayError

    class FakeClient:
        def call(self, **_kwargs):
            raise OpenClawGatewayError("ws closed")

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", True)
    monkeypatch.setattr(
        openclaw_gateway.settings, "openclaw_gateway_ws_fallback_to_cli", True
    )
    monkeypatch.setattr(openclaw_gateway, "_persistent_gateway_client", lambda: FakeClient())
    with caplog.at_level(logging.WARNING, logger="ai4all.openclaw_gateway"):
        with patch("app.platform.gateways.openclaw.subprocess.run", return_value=_Completed()) as mock_run:
            result = openclaw_gateway.send_weixin_text(
                to_user_id="peer@im.wechat",
                text="hello",
                idempotency_key="idem-fallback",
                gateway_timeout_ms=1234,
            )

    assert result["messageId"] == "msg-1"
    params = _params_from_call(mock_run)
    assert params["idempotencyKey"] == "idem-fallback"
    # 降级保留为 WARNING(不进飞书),但须带异常详情用于排障。
    fallback_warns = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "falling back to CLI" in rec.getMessage()
    ]
    assert fallback_warns, "WS→CLI 降级应记录 WARNING"
    assert "ws closed" in fallback_warns[0].getMessage()


def test_send_weixin_text_raises_ws_error_when_fallback_disabled(monkeypatch):
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawGatewayError

    class FakeClient:
        def call(self, **_kwargs):
            raise OpenClawGatewayError("ws auth failed")

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", True)
    monkeypatch.setattr(
        openclaw_gateway.settings, "openclaw_gateway_ws_fallback_to_cli", False
    )
    monkeypatch.setattr(openclaw_gateway, "_persistent_gateway_client", lambda: FakeClient())
    with patch("app.platform.gateways.openclaw.subprocess.run") as mock_run:
        with pytest.raises(OpenClawGatewayError, match="ws auth failed"):
            openclaw_gateway.send_weixin_text(
                to_user_id="peer@im.wechat",
                text="hello",
                gateway_timeout_ms=1234,
            )

    assert not mock_run.called


def test_send_weixin_text_preserves_rate_limit_semantics_for_ws(monkeypatch):
    from app.platform.gateways import openclaw as openclaw_gateway
    from app.platform.gateways.openclaw import OpenClawRateLimited

    class FakeClient:
        def call(self, **_kwargs):
            return {"ret": -2, "errmsg": "rate limited"}

    monkeypatch.setattr(openclaw_gateway.settings, "openclaw_gateway_ws_enabled", True)
    monkeypatch.setattr(openclaw_gateway, "_persistent_gateway_client", lambda: FakeClient())
    with patch("app.platform.gateways.openclaw.subprocess.run") as mock_run:
        with pytest.raises(OpenClawRateLimited):
            openclaw_gateway.send_weixin_text(
                to_user_id="peer@im.wechat",
                text="hello",
                gateway_timeout_ms=1234,
            )

    assert not mock_run.called


def test_logout_weixin_account_calls_openclaw_channels_logout():
    from app.platform.gateways.openclaw import logout_weixin_account

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=_LogoutCompleted()) as mock_run:
        result = logout_weixin_account(account_id="bot-im-bot", timeout_ms=1234)

    assert result["accountId"] == "bot-im-bot"
    assert result["channel"] == "openclaw-weixin"
    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "openclaw",
        "channels",
        "logout",
        "--channel",
        "openclaw-weixin",
        "--account",
        "bot-im-bot",
    ]


def test_logout_weixin_account_raises_gateway_error_on_cli_failure():
    from app.platform.gateways.openclaw import OpenClawGatewayError, logout_weixin_account

    class Failed:
        returncode = 1
        stdout = ""
        stderr = 'Channel logout failed: Error: Channel "openclaw-weixin" does not support logout.'

    with patch("app.platform.gateways.openclaw.subprocess.run", return_value=Failed()):
        with pytest.raises(OpenClawGatewayError, match="does not support logout"):
            logout_weixin_account(account_id="bot-im-bot", timeout_ms=1234)
