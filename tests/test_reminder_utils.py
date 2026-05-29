import pytest
from datetime import datetime
from app.reminder_utils import compute_next_due_at, validate_due_at, validate_recur_rule


def test_validate_due_at_standard_format():
    dt = validate_due_at("2026-06-01 10:00:00")
    assert dt == datetime(2026, 6, 1, 10, 0, 0)


def test_validate_due_at_iso_format():
    dt = validate_due_at("2026-06-01T10:00:00")
    assert dt == datetime(2026, 6, 1, 10, 0, 0)


def test_validate_due_at_invalid_raises():
    with pytest.raises(ValueError):
        validate_due_at("not-a-date")


def test_validate_recur_rule_daily():
    assert validate_recur_rule("daily") == "daily"


def test_validate_recur_rule_weekly():
    assert validate_recur_rule("weekly:6") == "weekly:6"


def test_validate_recur_rule_monthly():
    assert validate_recur_rule("monthly:15") == "monthly:15"


def test_validate_recur_rule_invalid_raises():
    with pytest.raises(ValueError):
        validate_recur_rule("hourly")


def test_compute_next_due_at_daily():
    base = datetime(2026, 5, 30, 9, 0, 0)
    nxt = compute_next_due_at("daily", base)
    assert nxt == datetime(2026, 5, 31, 9, 0, 0)


def test_compute_next_due_at_weekly():
    # base is Saturday (weekday=5), target is Sunday (6)
    base = datetime(2026, 5, 30, 9, 0, 0)  # Saturday
    nxt = compute_next_due_at("weekly:6", base)
    assert nxt == datetime(2026, 5, 31, 9, 0, 0)  # next Sunday


def test_compute_next_due_at_weekly_wraps():
    # base is Sunday (6), target is Monday (0): should be next Monday
    base = datetime(2026, 6, 7, 9, 0, 0)  # Sunday
    nxt = compute_next_due_at("weekly:0", base)
    assert nxt == datetime(2026, 6, 8, 9, 0, 0)  # next Monday


def test_compute_next_due_at_monthly():
    base = datetime(2026, 5, 15, 9, 0, 0)
    nxt = compute_next_due_at("monthly:15", base)
    assert nxt == datetime(2026, 6, 15, 9, 0, 0)


def test_compute_next_due_at_monthly_clamps_to_month_end():
    # monthly:31 in February → clamps to Feb 28
    base = datetime(2026, 1, 31, 9, 0, 0)
    nxt = compute_next_due_at("monthly:31", base)
    assert nxt.month == 2
    assert nxt.day == 28
