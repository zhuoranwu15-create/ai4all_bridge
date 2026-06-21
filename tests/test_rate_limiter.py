from app.rate_limiter import RateLimiter
from app.db._core import _tx


def test_allows_requests_under_limit(fresh_db):
    rl = RateLimiter()
    for _ in range(5):
        assert rl.check_rpm("acc", 5) is True


def test_blocks_at_limit(fresh_db):
    rl = RateLimiter()
    for _ in range(5):
        rl.check_rpm("acc", 5)
    assert rl.check_rpm("acc", 5) is False


def test_denied_request_not_recorded(fresh_db):
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc", 3)
    # At limit — two more denied calls should not increment the stored count
    rl.check_rpm("acc", 3)
    rl.check_rpm("acc", 3)
    with _tx(None) as tx:
        row = tx.execute(
            "SELECT COUNT(*) AS cnt FROM rpm_hits WHERE account_id = ?", ("acc",)
        ).fetchone()
    assert row["cnt"] == 3


def test_accounts_are_isolated(fresh_db):
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc_a", 3)
    assert rl.check_rpm("acc_a", 3) is False
    assert rl.check_rpm("acc_b", 3) is True


def test_zero_limit_means_no_limit(fresh_db):
    rl = RateLimiter()
    for _ in range(1000):
        assert rl.check_rpm("acc", 0) is True


def test_window_slides_after_configured_window(fresh_db):
    rl = RateLimiter()
    t0 = 1_000_000.0  # fixed reference epoch, avoids wall-clock dependency

    for i in range(3):
        rl.check_rpm("acc", 3, window_seconds=30, _now=t0 + i)
    assert rl.check_rpm("acc", 3, window_seconds=30, _now=t0 + 3) is False  # at limit

    # 35s after t0 — all previous hits fall outside the 30s window
    assert rl.check_rpm("acc", 3, window_seconds=30, _now=t0 + 35) is True
