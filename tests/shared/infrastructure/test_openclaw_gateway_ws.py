import json
from types import SimpleNamespace

import pytest


def _settings(**overrides):
    base = {
        "openclaw_gateway_ws_enabled": True,
        "openclaw_gateway_ws_url": "ws://127.0.0.1:18789",
        "openclaw_gateway_ws_token": "tok",
        "openclaw_gateway_ws_password": "",
        "openclaw_gateway_ws_config_path": "",
        "openclaw_gateway_ws_read_openclaw_config": False,
        "openclaw_gateway_ws_fallback_to_cli": True,
        "openclaw_gateway_ws_connect_timeout_ms": 3000,
        "openclaw_gateway_ws_request_timeout_ms": 5000,
        "openclaw_gateway_ws_protocol_version": 4,
        "openclaw_gateway_ws_warmup_on_startup": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _challenge(nonce="nonce-1"):
    return {"type": "event", "event": "connect.challenge", "payload": {"nonce": nonce}}


def _hello(*, protocol=4, methods=None, scopes=None):
    return {
        "type": "hello-ok",
        "protocol": protocol,
        "features": {
            "methods": ["send"] if methods is None else methods,
            "events": ["tick"],
        },
        "auth": {
            "role": "operator",
            "scopes": ["operator.write"] if scopes is None else scopes,
        },
    }


def _response_for_last(payload, *, ok=True, error=None):
    def _frame(ws):
        frame = {"type": "res", "id": ws.sent[-1]["id"], "ok": ok}
        if ok:
            frame["payload"] = payload
        else:
            frame["error"] = error or {"message": "failed"}
        return frame

    return _frame


class FakeWs:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    def recv(self, timeout=None):
        if not self.incoming:
            raise TimeoutError("no frame")
        item = self.incoming.pop(0)
        if callable(item):
            item = item(self)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return json.dumps(item)
        return item

    def send(self, message):
        self.sent.append(json.loads(message))

    def close(self):
        self.closed = True


def _client(incoming, settings=None):
    from app.platform.gateways.openclaw_ws import OpenClawPersistentGatewayClient

    created = []

    def _connect(url, **kwargs):
        ws = FakeWs(incoming)
        ws.url = url
        ws.connect_kwargs = kwargs
        created.append(ws)
        return ws

    client = OpenClawPersistentGatewayClient(
        settings or _settings(),
        connect_factory=_connect,
    )
    return client, created


def test_ws_client_consumes_challenge_and_sends_connect_then_send():
    client, created = _client(
        [
            _challenge(),
            _response_for_last(_hello()),
            _response_for_last({"messageId": "ws-1"}),
        ]
    )

    result = client.call(method="send", params={"message": "hi"}, timeout_ms=1234)

    assert result == {"messageId": "ws-1"}
    ws = created[0]
    assert ws.url == "ws://127.0.0.1:18789"
    assert ws.connect_kwargs["origin"] is None
    assert ws.connect_kwargs["proxy"] is None
    connect_frame = ws.sent[0]
    assert connect_frame["method"] == "connect"
    assert connect_frame["params"]["minProtocol"] == 4
    assert connect_frame["params"]["maxProtocol"] == 4
    assert connect_frame["params"]["client"]["id"] == "gateway-client"
    assert connect_frame["params"]["client"]["mode"] == "backend"
    assert connect_frame["params"]["scopes"] == ["operator.write"]
    assert connect_frame["params"]["auth"] == {"token": "tok"}
    assert ws.sent[1]["method"] == "send"
    assert ws.sent[1]["params"] == {"message": "hi"}


def test_ws_client_supports_password_auth():
    client, created = _client(
        [_challenge(), _response_for_last(_hello())],
        settings=_settings(openclaw_gateway_ws_token="", openclaw_gateway_ws_password="pw"),
    )

    client.warmup()

    assert created[0].sent[0]["params"]["auth"] == {"password": "pw"}


def test_ws_client_reads_password_mode_config_without_parsing_token_ref(tmp_path):
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "port": 18790,
                    "auth": {
                        "mode": "password",
                        "token": {"ref": "secret-token"},
                        "password": "pw-from-config",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    client, created = _client(
        [_challenge(), _response_for_last(_hello())],
        settings=_settings(
            openclaw_gateway_ws_url="",
            openclaw_gateway_ws_token="",
            openclaw_gateway_ws_password="",
            openclaw_gateway_ws_config_path=str(config_path),
            openclaw_gateway_ws_read_openclaw_config=True,
        ),
    )

    client.warmup()

    assert created[0].url == "ws://127.0.0.1:18790"
    assert created[0].sent[0]["params"]["auth"] == {"password": "pw-from-config"}


def test_ws_client_supports_auth_none_and_configured_protocol():
    client, created = _client(
        [_challenge(), _response_for_last(_hello(protocol=5))],
        settings=_settings(
            openclaw_gateway_ws_token="",
            openclaw_gateway_ws_password="",
            openclaw_gateway_ws_protocol_version=5,
        ),
    )

    client.warmup()

    params = created[0].sent[0]["params"]
    assert params["auth"] == {}
    assert params["minProtocol"] == 5
    assert params["maxProtocol"] == 5


def test_ws_client_rejects_missing_nonce_and_closes():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, created = _client([_challenge("")])

    with pytest.raises(OpenClawGatewayError, match="nonce"):
        client.call(method="send", params={}, timeout_ms=1234)

    assert created[0].sent == []
    assert created[0].closed is True


def test_ws_client_rejects_missing_operator_write_scope():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, created = _client(
        [_challenge(), _response_for_last(_hello(scopes=["operator.read"]))]
    )

    with pytest.raises(OpenClawGatewayError, match="operator.write"):
        client.call(method="send", params={}, timeout_ms=1234)

    assert created[0].closed is True


def test_ws_client_rejects_missing_send_method():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, created = _client(
        [_challenge(), _response_for_last(_hello(methods=["health"]))]
    )

    with pytest.raises(OpenClawGatewayError, match="send"):
        client.call(method="send", params={}, timeout_ms=1234)

    assert created[0].closed is True


def test_ws_client_surfaces_gateway_error_message():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, created = _client(
        [
            _challenge(),
            _response_for_last(_hello()),
            _response_for_last(None, ok=False, error={"message": "permission denied"}),
        ]
    )

    with pytest.raises(OpenClawGatewayError, match="permission denied"):
        client.call(method="send", params={}, timeout_ms=1234)

    assert created[0].closed is False


def test_ws_client_closes_on_invalid_json():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, created = _client([_challenge(), "not-json"])

    with pytest.raises(OpenClawGatewayError, match="invalid JSON"):
        client.call(method="send", params={}, timeout_ms=1234)

    assert created[0].closed is True


def test_ws_client_ignores_tick_while_waiting_for_response():
    client, _created = _client(
        [
            _challenge(),
            _response_for_last(_hello()),
            {"type": "event", "event": "tick", "payload": {"ts": 1}},
            _response_for_last({"messageId": "after-tick"}),
        ]
    )

    result = client.call(method="send", params={}, timeout_ms=1234)

    assert result == {"messageId": "after-tick"}


def test_ws_client_rejects_local_wss_without_pinned_tls():
    from app.platform.gateways.openclaw import OpenClawGatewayError

    client, _created = _client(
        [],
        settings=_settings(openclaw_gateway_ws_url="wss://127.0.0.1:18789"),
    )

    with pytest.raises(OpenClawGatewayError, match="local OpenClaw Gateway wss"):
        client.call(method="send", params={}, timeout_ms=1234)
