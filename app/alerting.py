import json
import logging
import re
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional


_FEISHU_ERROR_HANDLER_NAME = "ai4all_feishu_error_log_alert"


_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_BEARER_RE = re.compile(r"(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_WEBHOOK_RE = re.compile(r"https://[^\s\"']*/open-apis/bot/v2/hook/[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]+")
_TOKEN_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key[_-]?secret|secret|token|webhook)(\s*[:=]\s*)[^\s,;]+"
)


def redact_alert_text(text: str) -> str:
    """Redact common secrets and direct identifiers before sending operational alerts."""
    value = str(text or "")
    value = _WEBHOOK_RE.sub("https://[redacted-webhook]", value)
    value = _BEARER_RE.sub(r"\1[redacted]", value)

    def _redact_assignment(match: re.Match) -> str:
        return f"{match.group(1)}{match.group(2)}[redacted]"

    value = _TOKEN_ASSIGNMENT_RE.sub(_redact_assignment, value)

    def _redact_phone(match: re.Match) -> str:
        phone = match.group(1)
        return f"*******{phone[-4:]}"

    return _PHONE_RE.sub(_redact_phone, value)


def _send_feishu_text(webhook_url: str, text: str, timeout: float) -> None:
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


class FeishuErrorLogHandler(logging.Handler):
    """Send ai4all ERROR logs to Feishu with redaction and per-signature cooldown."""

    def __init__(
        self,
        *,
        webhook_url: str,
        app_env: str,
        min_interval_seconds: int = 300,
        timeout_seconds: float = 3.0,
        max_message_chars: int = 3500,
        sender: Optional[Callable[[str, str, float], None]] = None,
        async_send: bool = True,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self.webhook_url = webhook_url
        self.app_env = app_env
        self.min_interval_seconds = max(int(min_interval_seconds), 0)
        self.timeout_seconds = max(float(timeout_seconds), 0.5)
        self.max_message_chars = max(int(max_message_chars), 500)
        self.sender = sender or _send_feishu_text
        self.async_send = async_send
        self._last_sent_by_signature: dict[str, float] = {}
        self._lock = threading.Lock()
        self.name = _FEISHU_ERROR_HANDLER_NAME

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self.level:
                return
            signature = self._signature_for(record)
            if not self._should_send(signature):
                return
            message = self._format_alert(record)
            if self.async_send:
                thread = threading.Thread(target=self._send_safely, args=(message,), daemon=True)
                thread.start()
            else:
                self._send_safely(message)
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API name
        print("feishu error log alert handler failed", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    def _signature_for(self, record: logging.LogRecord) -> str:
        return "|".join(
            [
                str(record.name),
                str(record.levelno),
                str(record.msg),
                str(record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""),
            ]
        )

    def _should_send(self, signature: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last_sent = self._last_sent_by_signature.get(signature)
            if last_sent is not None and now - last_sent < self.min_interval_seconds:
                return False
            self._last_sent_by_signature[signature] = now
            return True

    def _format_alert(self, record: logging.LogRecord) -> str:
        lines = [
            "[AI4ALL][P2] business error log",
            f"env={self.app_env}",
            f"logger={record.name}",
            f"level={record.levelname}",
            f"logged_at={datetime.fromtimestamp(record.created).isoformat(timespec='seconds')}",
            f"message={record.getMessage()}",
        ]
        if record.exc_info:
            exc_text = logging.Formatter().formatException(record.exc_info)
            lines.append("exception=" + exc_text)
        text = redact_alert_text("\n".join(lines))
        if len(text) > self.max_message_chars:
            text = text[: self.max_message_chars - 20] + "\n...[truncated]"
        return text

    def _send_safely(self, message: str) -> None:
        try:
            self.sender(self.webhook_url, message, self.timeout_seconds)
        except Exception:
            print("feishu error log alert send failed", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)


def configure_error_log_alerting(settings: Any) -> bool:
    """Attach or remove the ai4all Feishu ERROR log handler based on runtime settings."""
    logger = logging.getLogger("ai4all")
    for handler in list(logger.handlers):
        if getattr(handler, "name", "") == _FEISHU_ERROR_HANDLER_NAME:
            logger.removeHandler(handler)
            handler.close()

    enabled = bool(getattr(settings, "feishu_error_log_alert_enabled", True))
    webhook_url = str(getattr(settings, "feishu_alert_webhook_url", "") or "").strip()
    if not enabled or not webhook_url:
        return False

    handler = FeishuErrorLogHandler(
        webhook_url=webhook_url,
        app_env=str(getattr(settings, "app_env", "") or ""),
        min_interval_seconds=int(getattr(settings, "feishu_error_log_alert_min_interval_seconds", 300)),
        timeout_seconds=float(getattr(settings, "feishu_error_log_alert_timeout_seconds", 3.0)),
        max_message_chars=int(getattr(settings, "feishu_error_log_alert_max_chars", 3500)),
    )
    logger.addHandler(handler)
    # Do NOT touch logger.level here. The handler itself filters at ERROR via
    # super().__init__(level=logging.ERROR), so attaching it is enough. Setting
    # the logger level was previously `min(level or INFO, INFO)` which silently
    # downgraded any operator-configured WARNING/ERROR threshold on the ai4all
    # namespace back to INFO at every restart.
    return True
