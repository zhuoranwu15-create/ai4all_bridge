import ipaddress
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

from app.openclaw_gateway import OpenClawGatewayError


logger = logging.getLogger("ai4all.openclaw_gateway_ws")

DEFAULT_GATEWAY_PORT = 18789
MAX_WS_PAYLOAD_BYTES = 25 * 1024 * 1024


class OpenClawGatewayWsDisabled(OpenClawGatewayError):
    """Raised when the persistent WS transport is disabled by configuration."""


class _GatewayWsTransportError(OpenClawGatewayError):
    """Internal marker for socket/protocol errors that require closing the connection."""


@dataclass
class _ResolvedGatewayWsConfig:
    url: str
    token: str
    password: str
    protocol_version: int
    connect_timeout_ms: int
    request_timeout_ms: int


def _setting_bool(settings: Any, name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    return value if isinstance(value, bool) else default


def _setting_str(settings: Any, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    return value.strip() if isinstance(value, str) else default


def _setting_int(settings: Any, name: str, default: int) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _parse_gateway_port_env(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.isdigit():
        port = int(value)
        return port if port > 0 else None
    if value.startswith("[") and "]:" in value:
        suffix = value.rsplit(":", 1)[-1]
        if suffix.isdigit():
            port = int(suffix)
            return port if port > 0 else None
    if value.count(":") == 1:
        suffix = value.rsplit(":", 1)[-1]
        if suffix.isdigit():
            port = int(suffix)
            return port if port > 0 else None
    return None


def _resolve_gateway_port(config: Dict[str, Any]) -> int:
    env_port = _parse_gateway_port_env(os.getenv("OPENCLAW_GATEWAY_PORT"))
    if env_port is not None:
        return env_port
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    configured = gateway.get("port")
    if isinstance(configured, bool):
        return DEFAULT_GATEWAY_PORT
    if isinstance(configured, int) and configured > 0:
        return configured
    return DEFAULT_GATEWAY_PORT


def _read_openclaw_config(path: str) -> Dict[str, Any]:
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise OpenClawGatewayError(f"OpenClaw config not found: {expanded}")
    try:
        with open(expanded, "r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except json.JSONDecodeError as err:
        raise OpenClawGatewayError(f"OpenClaw config is not valid JSON: {expanded}") from err
    except OSError as err:
        raise OpenClawGatewayError(f"OpenClaw config cannot be read: {expanded}") from err
    if not isinstance(parsed, dict):
        raise OpenClawGatewayError(f"OpenClaw config must be a JSON object: {expanded}")
    return parsed


def _plain_secret(value: Any, path: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    raise OpenClawGatewayError(f"{path} is not a plaintext string; SecretRef is unsupported")


def _resolve_auth(settings: Any, config: Dict[str, Any]) -> tuple[str, str]:
    explicit_token = _setting_str(settings, "openclaw_gateway_ws_token")
    explicit_password = _setting_str(settings, "openclaw_gateway_ws_password")
    if explicit_token and explicit_password:
        raise OpenClawGatewayError(
            "OPENCLAW_GATEWAY_WS_TOKEN and OPENCLAW_GATEWAY_WS_PASSWORD cannot both be set"
        )
    if explicit_token:
        return explicit_token, ""
    if explicit_password:
        return "", explicit_password

    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    auth = gateway.get("auth") if isinstance(gateway.get("auth"), dict) else {}
    mode = auth.get("mode")
    auth_mode = mode.strip().lower() if isinstance(mode, str) else ""

    if auth_mode == "none":
        return "", ""
    if auth_mode == "token":
        token = _plain_secret(auth.get("token"), "gateway.auth.token")
        if not token:
            raise OpenClawGatewayError("gateway.auth.mode=token but gateway.auth.token is empty")
        return token, ""
    if auth_mode == "password":
        password = _plain_secret(auth.get("password"), "gateway.auth.password")
        if not password:
            raise OpenClawGatewayError(
                "gateway.auth.mode=password but gateway.auth.password is empty"
            )
        return "", password
    if auth_mode and auth_mode != "trusted-proxy":
        raise OpenClawGatewayError(f"unsupported OpenClaw gateway auth mode: {auth_mode}")
    if auth_mode == "trusted-proxy":
        raise OpenClawGatewayError("gateway.auth.mode=trusted-proxy is unsupported")
    token = _plain_secret(auth.get("token"), "gateway.auth.token")
    password = _plain_secret(auth.get("password"), "gateway.auth.password")
    if token and password:
        raise OpenClawGatewayError(
            "gateway.auth.token and gateway.auth.password are both configured; set gateway.auth.mode"
        )
    return token, password


def _is_loopback_host(hostname: str) -> bool:
    host = hostname.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_gateway_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("ws", "wss"):
        raise OpenClawGatewayError("OpenClaw Gateway WS URL must use ws:// or wss://")
    if parsed.username or parsed.password:
        raise OpenClawGatewayError("OpenClaw Gateway WS URL must not include credentials")
    if parsed.path and parsed.path != "/":
        raise OpenClawGatewayError("OpenClaw Gateway WS URL path is unsupported")
    if parsed.query or parsed.fragment:
        raise OpenClawGatewayError("OpenClaw Gateway WS URL query/fragment is unsupported")
    if not parsed.hostname:
        raise OpenClawGatewayError("OpenClaw Gateway WS URL host is required")
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
        raise OpenClawGatewayError("plaintext OpenClaw Gateway ws:// is allowed only on loopback")
    if parsed.scheme == "wss" and _is_loopback_host(parsed.hostname):
        raise OpenClawGatewayError(
            "local OpenClaw Gateway wss:// requires pinned certificate support; use ws:// loopback or CLI fallback"
        )


def _resolve_gateway_ws_config(settings: Any) -> _ResolvedGatewayWsConfig:
    config: Dict[str, Any] = {}
    explicit_url = _setting_str(settings, "openclaw_gateway_ws_url")
    explicit_token = _setting_str(settings, "openclaw_gateway_ws_token")
    explicit_password = _setting_str(settings, "openclaw_gateway_ws_password")
    should_read_config = _setting_bool(settings, "openclaw_gateway_ws_read_openclaw_config", True)
    needs_config_for_url = not explicit_url
    needs_config_for_auth = not (explicit_token or explicit_password)
    if should_read_config and (needs_config_for_url or needs_config_for_auth):
        config_path = _setting_str(
            settings, "openclaw_gateway_ws_config_path", "~/.openclaw/openclaw.json"
        )
        config = _read_openclaw_config(config_path)

    if explicit_url:
        url = explicit_url
    else:
        gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
        tls = gateway.get("tls") if isinstance(gateway.get("tls"), dict) else {}
        scheme = "wss" if tls.get("enabled") is True else "ws"
        url = f"{scheme}://127.0.0.1:{_resolve_gateway_port(config)}"

    _validate_gateway_url(url)
    token, password = _resolve_auth(settings, config)
    protocol_version = max(1, _setting_int(settings, "openclaw_gateway_ws_protocol_version", 4))
    connect_timeout_ms = max(
        1, _setting_int(settings, "openclaw_gateway_ws_connect_timeout_ms", 3000)
    )
    request_timeout_ms = max(
        1, _setting_int(settings, "openclaw_gateway_ws_request_timeout_ms", 5000)
    )
    return _ResolvedGatewayWsConfig(
        url=url,
        token=token,
        password=password,
        protocol_version=protocol_version,
        connect_timeout_ms=connect_timeout_ms,
        request_timeout_ms=request_timeout_ms,
    )


def _request_timeout_seconds(config: _ResolvedGatewayWsConfig, timeout_ms: int) -> float:
    raw_timeout = (
        timeout_ms if isinstance(timeout_ms, int) and timeout_ms > 0 else config.request_timeout_ms
    )
    return max(0.001, min(raw_timeout, config.request_timeout_ms) / 1000.0)


class OpenClawPersistentGatewayClient:
    """Synchronous persistent OpenClaw Gateway WS client for outbound send RPCs."""

    def __init__(
        self,
        settings: Any,
        *,
        connect_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._settings = settings
        self._connect_factory = connect_factory or websocket_connect
        self._lock = threading.RLock()
        self._ws: Optional[Any] = None
        self._config: Optional[_ResolvedGatewayWsConfig] = None
        self._instance_id = f"ai4all-{uuid4()}"

    def call(self, *, method: str, params: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        """Call one Gateway RPC over the persistent socket, reconnecting when needed."""
        if not _setting_bool(self._settings, "openclaw_gateway_ws_enabled", False):
            raise OpenClawGatewayWsDisabled("persistent OpenClaw Gateway WS is disabled")
        with self._lock:
            try:
                self._ensure_connected_locked()
                assert self._config is not None
                payload = self._send_request_locked(
                    method=method,
                    params=params,
                    timeout_seconds=_request_timeout_seconds(self._config, timeout_ms),
                )
            except _GatewayWsTransportError:
                self._close_locked()
                raise
            except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError) as err:
                self._close_locked()
                raise _GatewayWsTransportError(f"OpenClaw Gateway WS transport failed: {err}") from err
            if not isinstance(payload, dict):
                raise OpenClawGatewayError(
                    f"OpenClaw Gateway WS returned invalid payload for {method}"
                )
            return payload

    def warmup(self) -> None:
        """Open and authenticate the persistent Gateway WS connection if enabled."""
        if not _setting_bool(self._settings, "openclaw_gateway_ws_enabled", False):
            return
        with self._lock:
            try:
                self._ensure_connected_locked()
            except _GatewayWsTransportError:
                self._close_locked()
                raise

    def close(self) -> None:
        """Close the persistent Gateway WS connection."""
        with self._lock:
            self._close_locked()

    def _ensure_connected_locked(self) -> None:
        if self._ws is not None:
            return
        config = _resolve_gateway_ws_config(self._settings)
        timeout_seconds = config.connect_timeout_ms / 1000.0
        try:
            ws = self._connect_factory(
                config.url,
                open_timeout=timeout_seconds,
                max_size=MAX_WS_PAYLOAD_BYTES,
                origin=None,
                proxy=None,
            )
        except Exception as err:
            raise _GatewayWsTransportError(
                f"OpenClaw Gateway WS connect failed: {err}"
            ) from err

        self._ws = ws
        self._config = config
        try:
            self._read_connect_challenge_locked(timeout_seconds)
            connect_payload = self._send_connect_locked(timeout_seconds)
            self._validate_hello_ok(connect_payload, config.protocol_version)
        except Exception:
            self._close_locked()
            raise
        logger.info(
            "persistent OpenClaw Gateway WS connected url=%s protocol=%s",
            config.url,
            config.protocol_version,
        )

    def _read_connect_challenge_locked(self, timeout_seconds: float) -> str:
        deadline = time.monotonic() + timeout_seconds
        while True:
            frame = self._recv_frame_locked(deadline)
            if frame.get("type") != "event":
                raise _GatewayWsTransportError("expected connect.challenge event from Gateway")
            if frame.get("event") == "tick":
                continue
            if frame.get("event") != "connect.challenge":
                raise _GatewayWsTransportError(
                    f"unexpected Gateway event before connect: {frame.get('event')}"
                )
            payload = frame.get("payload")
            nonce = payload.get("nonce") if isinstance(payload, dict) else None
            if not isinstance(nonce, str) or not nonce.strip():
                raise _GatewayWsTransportError("Gateway connect.challenge missing nonce")
            return nonce.strip()

    def _send_connect_locked(self, timeout_seconds: float) -> Dict[str, Any]:
        assert self._config is not None
        auth: Dict[str, str] = {}
        if self._config.token:
            auth["token"] = self._config.token
        elif self._config.password:
            auth["password"] = self._config.password
        params: Dict[str, Any] = {
            "minProtocol": self._config.protocol_version,
            "maxProtocol": self._config.protocol_version,
            "client": {
                "id": "gateway-client",
                "displayName": "ai4all-bridge",
                "version": "ai4all",
                "platform": sys.platform,
                "mode": "backend",
                "instanceId": self._instance_id,
            },
            "caps": [],
            "role": "operator",
            "scopes": ["operator.write"],
            "auth": auth,
        }
        payload = self._send_request_locked(
            method="connect",
            params=params,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise _GatewayWsTransportError("OpenClaw Gateway connect returned invalid payload")
        return payload

    def _send_request_locked(
        self,
        *,
        method: str,
        params: Dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        if self._ws is None:
            raise _GatewayWsTransportError("Gateway WS is not connected")
        request_id = str(uuid4())
        frame = {
            "type": "req",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as err:
            raise _GatewayWsTransportError(
                f"OpenClaw Gateway WS send failed for {method}: {err}"
            ) from err
        return self._recv_response_locked(
            request_id=request_id,
            method=method,
            timeout_seconds=timeout_seconds,
        )

    def _recv_response_locked(
        self,
        *,
        request_id: str,
        method: str,
        timeout_seconds: float,
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds
        while True:
            frame = self._recv_frame_locked(deadline)
            frame_type = frame.get("type")
            if frame_type == "event":
                if frame.get("event") == "tick":
                    continue
                logger.debug(
                    "ignoring Gateway event while waiting for %s: %s",
                    method,
                    frame.get("event"),
                )
                continue
            if frame_type != "res":
                raise _GatewayWsTransportError(f"unexpected Gateway frame type: {frame_type}")
            if frame.get("id") != request_id:
                raise _GatewayWsTransportError(
                    f"unexpected Gateway response id while waiting for {method}"
                )
            if frame.get("ok") is True:
                return frame.get("payload")
            error = frame.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise OpenClawGatewayError(message or f"OpenClaw Gateway WS call failed: {method}")

    def _recv_frame_locked(self, deadline: float) -> Dict[str, Any]:
        if self._ws is None:
            raise _GatewayWsTransportError("Gateway WS is not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _GatewayWsTransportError("OpenClaw Gateway WS request timed out")
        try:
            raw = self._ws.recv(timeout=remaining)
        except TimeoutError as err:
            raise _GatewayWsTransportError("OpenClaw Gateway WS request timed out") from err
        except Exception as err:
            raise _GatewayWsTransportError(
                f"OpenClaw Gateway WS receive failed: {err}"
            ) from err
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise _GatewayWsTransportError("OpenClaw Gateway WS returned non-text frame")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as err:
            raise _GatewayWsTransportError("OpenClaw Gateway WS returned invalid JSON") from err
        if not isinstance(frame, dict):
            raise _GatewayWsTransportError("OpenClaw Gateway WS returned non-object frame")
        return frame

    def _validate_hello_ok(self, payload: Dict[str, Any], protocol_version: int) -> None:
        if not isinstance(payload, dict):
            raise _GatewayWsTransportError("OpenClaw Gateway connect returned invalid payload")
        if payload.get("type") != "hello-ok":
            raise _GatewayWsTransportError("OpenClaw Gateway connect did not return hello-ok")
        if payload.get("protocol") != protocol_version:
            raise _GatewayWsTransportError("OpenClaw Gateway protocol mismatch")
        features = payload.get("features")
        methods = features.get("methods") if isinstance(features, dict) else None
        if not isinstance(methods, list) or "send" not in methods:
            raise _GatewayWsTransportError("OpenClaw Gateway does not advertise send method")
        auth = payload.get("auth")
        scopes = auth.get("scopes") if isinstance(auth, dict) else None
        if not isinstance(scopes, list) or not (
            "operator.write" in scopes or "operator.admin" in scopes
        ):
            raise _GatewayWsTransportError("OpenClaw Gateway did not grant operator.write")

    def _close_locked(self) -> None:
        ws = self._ws
        self._ws = None
        self._config = None
        if ws is None:
            return
        try:
            ws.close()
        except Exception as err:
            logger.debug("OpenClaw Gateway WS close failed: %s", err)
