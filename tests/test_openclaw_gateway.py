import json
from unittest.mock import patch

import pytest


class _Completed:
    returncode = 0
    stdout = '{"messageId":"msg-1","channel":"openclaw-weixin"}'
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
