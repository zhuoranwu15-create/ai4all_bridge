import logging
from types import SimpleNamespace


def test_feishu_error_log_handler_sends_error_log():
    from app.alerting import FeishuErrorLogHandler

    sent = []

    def sender(webhook_url, text, timeout):
        sent.append((webhook_url, text, timeout))

    handler = FeishuErrorLogHandler(
        webhook_url="https://example.test/hook",
        app_env="test",
        min_interval_seconds=300,
        sender=sender,
        async_send=False,
    )
    record = logging.LogRecord(
        name="ai4all.sms",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="sms failed phone=%s",
        args=("13820360155",),
        exc_info=None,
    )

    handler.handle(record)

    assert len(sent) == 1
    assert sent[0][0] == "https://example.test/hook"
    assert "[AI4ALL][P2] business error log" in sent[0][1]
    assert "logger=ai4all.sms" in sent[0][1]
    assert "*******0155" in sent[0][1]
    assert "13820360155" not in sent[0][1]


def test_feishu_error_log_handler_ignores_warning():
    from app.alerting import FeishuErrorLogHandler

    sent = []
    handler = FeishuErrorLogHandler(
        webhook_url="https://example.test/hook",
        app_env="test",
        sender=lambda webhook_url, text, timeout: sent.append(text),
        async_send=False,
    )
    record = logging.LogRecord(
        name="ai4all.sms",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="warn only",
        args=(),
        exc_info=None,
    )

    handler.handle(record)

    assert sent == []


def test_feishu_error_log_handler_suppresses_same_signature_within_cooldown():
    from app.alerting import FeishuErrorLogHandler

    sent = []
    handler = FeishuErrorLogHandler(
        webhook_url="https://example.test/hook",
        app_env="test",
        min_interval_seconds=300,
        sender=lambda webhook_url, text, timeout: sent.append(text),
        async_send=False,
    )
    first = logging.LogRecord(
        name="ai4all.sms",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="sms failed phone=%s",
        args=("13820360155",),
        exc_info=None,
    )
    second = logging.LogRecord(
        name="ai4all.sms",
        level=logging.ERROR,
        pathname=__file__,
        lineno=11,
        msg="sms failed phone=%s",
        args=("13820369999",),
        exc_info=None,
    )

    handler.handle(first)
    handler.handle(second)

    assert len(sent) == 1


def test_redact_alert_text_masks_secrets_and_webhooks():
    from app.alerting import redact_alert_text

    text = (
        "Authorization: Bearer abc.def "
        "access_key_secret=super-secret "
        "https://open.feishu.cn/open-apis/bot/v2/hook/token-value "
        "phone=13820360155"
    )

    redacted = redact_alert_text(text)

    assert "Bearer [redacted]" in redacted
    assert "access_key_secret=[redacted]" in redacted
    assert "https://[redacted-webhook]" in redacted
    assert "*******0155" in redacted
    assert "super-secret" not in redacted
    assert "13820360155" not in redacted


def test_configure_error_log_alerting_requires_webhook():
    from app.alerting import _FEISHU_ERROR_HANDLER_NAME, configure_error_log_alerting

    logger = logging.getLogger("ai4all")
    settings = SimpleNamespace(feishu_error_log_alert_enabled=True, feishu_alert_webhook_url="")

    configured = configure_error_log_alerting(settings)

    assert configured is False
    assert not any(getattr(handler, "name", "") == _FEISHU_ERROR_HANDLER_NAME for handler in logger.handlers)


def test_configure_error_log_alerting_does_not_change_logger_level():
    from app.alerting import _FEISHU_ERROR_HANDLER_NAME, configure_error_log_alerting

    logger = logging.getLogger("ai4all")
    original_level = logger.level
    logger.setLevel(logging.WARNING)
    settings = SimpleNamespace(
        feishu_error_log_alert_enabled=True,
        feishu_alert_webhook_url="https://example.test/hook",
        app_env="test",
        feishu_error_log_alert_min_interval_seconds=300,
        feishu_error_log_alert_timeout_seconds=3.0,
        feishu_error_log_alert_max_chars=3500,
    )
    try:
        assert configure_error_log_alerting(settings) is True
        # Operator-configured WARNING must NOT be silently downgraded to INFO.
        assert logger.level == logging.WARNING
    finally:
        for handler in list(logger.handlers):
            if getattr(handler, "name", "") == _FEISHU_ERROR_HANDLER_NAME:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(original_level)


def test_configure_error_log_alerting_replaces_existing_handler():
    from app.alerting import _FEISHU_ERROR_HANDLER_NAME, configure_error_log_alerting

    logger = logging.getLogger("ai4all")
    settings = SimpleNamespace(
        feishu_error_log_alert_enabled=True,
        feishu_alert_webhook_url="https://example.test/hook",
        app_env="test",
        feishu_error_log_alert_min_interval_seconds=300,
        feishu_error_log_alert_timeout_seconds=3.0,
        feishu_error_log_alert_max_chars=3500,
    )
    try:
        assert configure_error_log_alerting(settings) is True
        assert configure_error_log_alerting(settings) is True
        handlers = [
            handler for handler in logger.handlers
            if getattr(handler, "name", "") == _FEISHU_ERROR_HANDLER_NAME
        ]
        assert len(handlers) == 1
    finally:
        for handler in list(logger.handlers):
            if getattr(handler, "name", "") == _FEISHU_ERROR_HANDLER_NAME:
                logger.removeHandler(handler)
                handler.close()
