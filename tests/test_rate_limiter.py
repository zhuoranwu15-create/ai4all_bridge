import time
from app.rate_limiter import RateLimiter


def test_allows_requests_under_limit():
    rl = RateLimiter()
    for _ in range(5):
        assert rl.check_rpm("acc", 5) is True


def test_blocks_at_limit():
    rl = RateLimiter()
    for _ in range(5):
        rl.check_rpm("acc", 5)
    assert rl.check_rpm("acc", 5) is False


def test_denied_request_not_recorded():
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc", 3)
    # At limit — denied, should not advance the window
    rl.check_rpm("acc", 3)
    rl.check_rpm("acc", 3)
    # Still exactly 3 recorded
    assert len(rl._windows["acc"]) == 3


def test_accounts_are_isolated():
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc_a", 3)
    # acc_a is at limit
    assert rl.check_rpm("acc_a", 3) is False
    # acc_b is unaffected
    assert rl.check_rpm("acc_b", 3) is True


def test_zero_limit_means_no_limit():
    rl = RateLimiter()
    for _ in range(1000):
        assert rl.check_rpm("acc", 0) is True


def test_window_slides_after_configured_window(monkeypatch):
    import time as time_module
    rl = RateLimiter()
    now = [0.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: now[0])

    for _ in range(3):
        rl.check_rpm("acc", 3, window_seconds=30)
    assert rl.check_rpm("acc", 3, window_seconds=30) is False  # at limit

    now[0] = 31.0  # advance past the configured 30s window
    assert rl.check_rpm("acc", 3, window_seconds=30) is True  # old entries expired
