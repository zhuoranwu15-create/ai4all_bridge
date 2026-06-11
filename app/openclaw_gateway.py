import base64
import io
import json
import re
import subprocess
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import qrcode


class OpenClawGatewayError(RuntimeError):
    pass


class OpenClawRateLimited(OpenClawGatewayError):
    """发送被 iLink/网关限速（典型 ret=-2 / errmsg=rate limited）。

    单独成类，便于调用方退避重试并与一般失败区分（一般失败立即落 failed，
    限速则可按账号退避后重试）。``ret`` 为网关返回的业务码，未知时为 None。
    """

    def __init__(self, message: str, *, ret: Optional[int] = None) -> None:
        super().__init__(message)
        self.ret = ret


DEFAULT_WEIXIN_CHANNEL = "openclaw-weixin"

# 限速文案匹配（errmsg=rate limited / rate_limited 等大小写变体）。
_RATE_LIMIT_PATTERN = re.compile(r"rate.?limit", re.IGNORECASE)
# iLink 限速的业务返回码。
_RATE_LIMIT_RET = -2


def _extract_send_result_error(result: Dict[str, Any]) -> Optional[Tuple[Optional[int], str]]:
    """从 send 返回体里抽取业务错误 ``(code, message)``；无错误返回 None。

    关键背景：``openclaw gateway call send`` 即使 CLI 退出码为 0，iLink 仍可能在
    返回体里携带业务错误，最典型是限速 ``ret=-2 / errmsg=rate limited``。旧逻辑只读
    ``messageId``、不看业务码，会把被限速的消息误标为已发送（静默丢消息）。

    判定原则（容错、零回归）：
    - 拿到非空 ``messageId`` → 视为网关已受理，直接判成功（None）。
    - 否则在已知 iLink/网关字段里找非零业务码或限速文案；命中才报错。
    - 字段名按已知 iLink 形态做兼容匹配；若真实返回字段不同，则不命中、退回旧行为，
      不会把正常成功误判为失败。生产抓到一次真实限速返回后应据此校正字段名。
    """
    if not isinstance(result, dict):
        return None
    # 成功信号优先：网关回了 messageId 即已受理。
    if str(result.get("messageId") or "").strip():
        return None

    code: Optional[int] = None
    for key in ("ret", "errcode", "code"):
        value = result.get(key)
        if isinstance(value, bool):  # bool 是 int 子类，需先排除
            continue
        if isinstance(value, int):
            code = value
            break
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            code = int(value.strip())
            break

    message = ""
    for key in ("errmsg", "error", "message", "msg"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break

    if code is not None and code != 0:
        return code, message or f"gateway returned ret={code}"
    if message and _RATE_LIMIT_PATTERN.search(message):
        return code, message
    return None


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


def logout_weixin_account(
    *,
    account_id: str,
    channel: str = DEFAULT_WEIXIN_CHANNEL,
    timeout_ms: int,
) -> Dict[str, Any]:
    resolved_account_id = account_id.strip() if account_id else ""
    resolved_channel = channel.strip() if channel else DEFAULT_WEIXIN_CHANNEL
    if not resolved_account_id:
        raise ValueError("account_id is required")
    if not resolved_channel:
        resolved_channel = DEFAULT_WEIXIN_CHANNEL

    cmd = [
        "openclaw",
        "channels",
        "logout",
        "--channel",
        resolved_channel,
        "--account",
        resolved_account_id,
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
        raise OpenClawGatewayError(
            f"OpenClaw channel logout timed out: {resolved_channel}/{resolved_account_id}"
        ) from err
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OpenClawGatewayError(
            detail or f"OpenClaw channel logout failed: {resolved_channel}/{resolved_account_id}"
        )
    return {
        "channel": resolved_channel,
        "accountId": resolved_account_id,
        "stdout": (completed.stdout or "").strip(),
    }


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

    result = _run_gateway_call(
        method="send",
        params=params,
        timeout_ms=gateway_timeout_ms,
    )
    # CLI 退出码为 0 不代表发送成功：iLink 可能在返回体里带业务错误（如限速 ret=-2）。
    # 识别后抛出，避免被上层误标为已发送。限速单独抛 OpenClawRateLimited 以便退避重试。
    send_error = _extract_send_result_error(result)
    if send_error is not None:
        ret_code, error_message = send_error
        if ret_code == _RATE_LIMIT_RET or _RATE_LIMIT_PATTERN.search(error_message):
            raise OpenClawRateLimited(error_message, ret=ret_code)
        raise OpenClawGatewayError(error_message)
    return result
