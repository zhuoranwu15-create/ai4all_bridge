import json
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


def test_send_weixin_text_calls_gateway_send_with_explicit_route():
    from app.openclaw_gateway import send_weixin_text

    with patch("app.openclaw_gateway.subprocess.run", return_value=_Completed()) as mock_run:
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


def test_send_weixin_text_generates_idempotency_key_when_missing():
    from app.openclaw_gateway import send_weixin_text

    with patch("app.openclaw_gateway.subprocess.run", return_value=_Completed()) as mock_run:
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
    from app.openclaw_gateway import send_weixin_text

    with pytest.raises(ValueError, match=message):
        send_weixin_text(
            to_user_id=to_user_id,
            text=text,
            gateway_timeout_ms=1234,
        )


def test_send_weixin_text_raises_rate_limited_on_ret_minus_2():
    """CLI 退出码 0 但返回体带 ret=-2 → 抛 OpenClawRateLimited（不再静默标 sent）。"""
    from app.openclaw_gateway import OpenClawRateLimited, send_weixin_text

    class RateLimited:
        returncode = 0
        stdout = '{"ret":-2,"errmsg":"rate limited"}'
        stderr = ""

    with patch("app.openclaw_gateway.subprocess.run", return_value=RateLimited()):
        with pytest.raises(OpenClawRateLimited) as exc_info:
            send_weixin_text(
                to_user_id="peer@im.wechat",
                text="hello",
                gateway_timeout_ms=1234,
            )
    assert exc_info.value.ret == -2


def test_send_weixin_text_raises_rate_limited_on_errmsg_only():
    """无业务码、仅 errmsg 含 rate limit 也应识别为限速。"""
    from app.openclaw_gateway import OpenClawRateLimited, send_weixin_text

    class RateLimited:
        returncode = 0
        stdout = '{"error":"send blocked: Rate_Limited, retry later"}'
        stderr = ""

    with patch("app.openclaw_gateway.subprocess.run", return_value=RateLimited()):
        with pytest.raises(OpenClawRateLimited):
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)


def test_send_weixin_text_raises_gateway_error_on_nonzero_ret():
    """非限速的非零业务码 → 抛普通 OpenClawGatewayError（非限速，不退避）。"""
    from app.openclaw_gateway import OpenClawGatewayError, OpenClawRateLimited, send_weixin_text

    class Failed:
        returncode = 0
        stdout = '{"ret":500,"errmsg":"internal error"}'
        stderr = ""

    with patch("app.openclaw_gateway.subprocess.run", return_value=Failed()):
        with pytest.raises(OpenClawGatewayError) as exc_info:
            send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    # 非限速类不应被识别成 RateLimited
    assert not isinstance(exc_info.value, OpenClawRateLimited)


def test_send_weixin_text_success_with_messageid_not_treated_as_error():
    """有 messageId 即成功，即使返回体里同时带 ret=0 也不误判。"""
    from app.openclaw_gateway import send_weixin_text

    class Ok:
        returncode = 0
        stdout = '{"messageId":"m-ok","ret":0}'
        stderr = ""

    with patch("app.openclaw_gateway.subprocess.run", return_value=Ok()):
        result = send_weixin_text(to_user_id="peer@im.wechat", text="hi", gateway_timeout_ms=1234)
    assert result["messageId"] == "m-ok"


def test_logout_weixin_account_calls_openclaw_channels_logout():
    from app.openclaw_gateway import logout_weixin_account

    with patch("app.openclaw_gateway.subprocess.run", return_value=_LogoutCompleted()) as mock_run:
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
    from app.openclaw_gateway import OpenClawGatewayError, logout_weixin_account

    class Failed:
        returncode = 1
        stdout = ""
        stderr = 'Channel logout failed: Error: Channel "openclaw-weixin" does not support logout.'

    with patch("app.openclaw_gateway.subprocess.run", return_value=Failed()):
        with pytest.raises(OpenClawGatewayError, match="does not support logout"):
            logout_weixin_account(account_id="bot-im-bot", timeout_ms=1234)
