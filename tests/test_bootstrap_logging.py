import logging

from app.bootstrap.application import _configure_logging


def test_httpx_info_request_urls_are_suppressed():
    logger = logging.getLogger("httpx")
    original_level = logger.level
    try:
        logger.setLevel(logging.INFO)
        _configure_logging()
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(original_level)
