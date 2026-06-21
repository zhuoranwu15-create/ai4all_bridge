from app.rate_limiter import RateLimiter, _advisory_key
from app.db._core import _tx


def test_advisory_key_is_stable_and_in_int64_range():
    """advisory 锁键须跨进程稳定且落在 PG bigint(int64) 范围内。"""
    k1 = _advisory_key("acc")
    k2 = _advisory_key("acc")
    assert k1 == k2  # 确定性
    assert _advisory_key("acc_a") != _advisory_key("acc_b")  # 不同账号不同键
    lo, hi = -(2 ** 63), 2 ** 63 - 1
    for acc in ("acc", "aid_265610123", "x" * 200, "中文账号"):
        assert lo <= _advisory_key(acc) <= hi


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
