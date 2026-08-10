import pytest
from datetime import datetime, time
from app.reminder_utils import (
    compute_first_due_at,
    compute_next_due_at,
    validate_due_at,
    validate_recur_rule,
)


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


# --- 多星期几扩展 ---

def test_validate_recur_rule_weekly_multi_normalizes():
    # 去重 + 升序归一
    assert validate_recur_rule("weekly:4,0,2,2") == "weekly:0,2,4"


def test_validate_recur_rule_weekly_single_backward_compat():
    assert validate_recur_rule("weekly:3") == "weekly:3"


def test_validate_recur_rule_weekly_out_of_range_raises():
    with pytest.raises(ValueError):
        validate_recur_rule("weekly:7")


def test_validate_recur_rule_weekly_empty_segment_raises():
    with pytest.raises(ValueError):
        validate_recur_rule("weekly:0,")


def test_compute_next_due_at_weekly_multi_picks_nearest():
    # 「一三五」= weekly:0,2,4；base 为周三 08:00 → 下一命中应为周五 08:00
    base = datetime(2026, 7, 15, 8, 0, 0)  # Wednesday
    assert base.weekday() == 2
    nxt = compute_next_due_at("weekly:0,2,4", base)
    assert nxt == datetime(2026, 7, 17, 8, 0, 0)  # Friday


def test_compute_next_due_at_weekly_multi_wraps_to_next_week():
    # base 为周五 08:00 → 「一三五」下一命中应为下周一
    base = datetime(2026, 7, 17, 8, 0, 0)  # Friday
    nxt = compute_next_due_at("weekly:0,2,4", base)
    assert nxt == datetime(2026, 7, 20, 8, 0, 0)  # next Monday


# --- compute_first_due_at：后端算首次触发，绝不信 LLM 日期 ---

def test_compute_first_due_at_weekly_today_passed_picks_next_matching_day():
    # 今天周三，已过 08:00 → 「一三五 08:00」首次应为周五 08:00
    now = datetime(2026, 7, 15, 9, 30, 0)  # Wednesday, past 08:00
    first = compute_first_due_at("weekly:0,2,4", time(8, 0, 0), now)
    assert first == datetime(2026, 7, 17, 8, 0, 0)


def test_compute_first_due_at_weekly_today_upcoming_uses_today():
    # 今天周三，未到 08:00 → 首次就是今天 08:00
    now = datetime(2026, 7, 15, 6, 0, 0)  # Wednesday, before 08:00
    first = compute_first_due_at("weekly:0,2,4", time(8, 0, 0), now)
    assert first == datetime(2026, 7, 15, 8, 0, 0)


def test_compute_first_due_at_daily():
    now = datetime(2026, 7, 15, 9, 0, 0)
    assert compute_first_due_at("daily", time(8, 0, 0), now) == datetime(2026, 7, 16, 8, 0, 0)
    assert compute_first_due_at("daily", time(10, 0, 0), now) == datetime(2026, 7, 15, 10, 0, 0)
