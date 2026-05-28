import base64
import io
import json
import subprocess
from typing import Any, Dict, Optional
from uuid import uuid4

import qrcode


class OpenClawGatewayError(RuntimeError):
    pass


DEFAULT_WEIXIN_CHANNEL = "openclaw-weixin"


def _run_gateway_call(
    *,
    method: str,
    params: Dict[str, Any],
    timeout_ms: int,
) -> Dict[str, Any]:
    cmd = [
        "openclaw",
        "gateway",
        "call",
        method,
        "--json",
        "--timeout",
        str(timeout_ms),
        "--params",
        json.dumps(params, ensure_ascii=False),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout_ms / 1000 + 5, 10),
        )
    except subprocess.TimeoutExpired as err:
        raise OpenClawGatewayError(f"OpenClaw Gateway call timed out: {method}") from err
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OpenClawGatewayError(detail or f"OpenClaw Gateway call failed: {method}")
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as err:
        raise OpenClawGatewayError(
            f"OpenClaw Gateway returned non-JSON output for {method}"
        ) from err
    if not isinstance(parsed, dict):
        raise OpenClawGatewayError(f"OpenClaw Gateway returned invalid result for {method}")
    return parsed


def _render_qr_payload_to_data_url(payload: str) -> str:
    img = qrcode.make(payload, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def normalize_qr_data_url(raw_value: Optional[str]) -> Optional[str]:
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        return value
    return _render_qr_payload_to_data_url(value)


def start_weixin_qr_login(
    *,
    account_id: str,
    gateway_timeout_ms: int,
    start_timeout_ms: int,
    force: bool = False,
) -> Dict[str, Any]:
    result = _run_gateway_call(
        method="web.login.start",
        params={
            "accountId": account_id,
            "force": force,
            "timeoutMs": start_timeout_ms,
            "verbose": False,
        },
        timeout_ms=gateway_timeout_ms,
    )
    raw_qr = result.get("qrDataUrl")
    qr_data_url = normalize_qr_data_url(raw_qr if isinstance(raw_qr, str) else None)
    return {
        **result,
        "rawQrDataUrl": raw_qr,
        "qrDataUrl": qr_data_url,
    }


def wait_weixin_qr_login(
    *,
    account_id: str,
    current_qr_data_url: Optional[str],
    gateway_timeout_ms: int,
    wait_timeout_ms: int,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "accountId": account_id,
        "timeoutMs": wait_timeout_ms,
    }
    if current_qr_data_url:
        params["currentQrDataUrl"] = current_qr_data_url
    return _run_gateway_call(
        method="web.login.wait",
        params=params,
        timeout_ms=max(gateway_timeout_ms, wait_timeout_ms + 5000),
    )


def send_weixin_text(
    *,
    to_user_id: str,
    text: str,
    gateway_timeout_ms: int,
    account_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    session_key: Optional[str] = None,
    channel: str = DEFAULT_WEIXIN_CHANNEL,
) -> Dict[str, Any]:
    """Send proactive Weixin text through OpenClaw Gateway's generic send RPC."""
    target = to_user_id.strip()
    message = text.strip()
    if not target:
        raise ValueError("to_user_id is required")
    if not message:
        raise ValueError("text is required")
    resolved_channel = channel.strip() if channel else ""
    if not resolved_channel:
        resolved_channel = DEFAULT_WEIXIN_CHANNEL
    resolved_idempotency_key = idempotency_key.strip() if idempotency_key else ""
    if not resolved_idempotency_key:
        resolved_idempotency_key = f"ai4all-send-{uuid4()}"

    params: Dict[str, Any] = {
        "channel": resolved_channel,
        "to": target,
        "message": message,
        "idempotencyKey": resolved_idempotency_key,
    }
    if account_id and account_id.strip():
        params["accountId"] = account_id.strip()
    if session_key and session_key.strip():
        params["sessionKey"] = session_key.strip()

    return _run_gateway_call(
        method="send",
        params=params,
        timeout_ms=gateway_timeout_ms,
    )
