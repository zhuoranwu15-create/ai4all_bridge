from datetime import datetime

from app.reminder_parser import parse_explicit_reminder


def test_parse_relative_explicit_reminder():
    parsed = parse_explicit_reminder(
        "明天上午10点提醒我检查事情A",
        now=datetime(2026, 5, 22, 9, 0),
    )

    assert parsed is not None
    assert parsed.due_at_db == "2026-05-23 10:00:00"
    assert parsed.text == "检查事情A"


def test_parse_absolute_datetime_reminder():
    parsed = parse_explicit_reminder(
        "提醒我2026-05-23 18:30去趟派出所",
        now=datetime(2026, 5, 22, 9, 0),
    )

    assert parsed is not None
    assert parsed.due_at_db == "2026-05-23 18:30:00"
    assert parsed.text == "去趟派出所"


def test_parse_rejects_missing_specific_time():
    parsed = parse_explicit_reminder(
        "下午提醒我去趟派出所",
        now=datetime(2026, 5, 22, 9, 0),
    )

    assert parsed is None


def test_parse_rejects_ambiguous_hour_without_period():
    parsed = parse_explicit_reminder(
        "明天10点提醒我检查事情A",
        now=datetime(2026, 5, 22, 9, 0),
    )

    assert parsed is None


def test_parse_rejects_past_due_time():
    parsed = parse_explicit_reminder(
        "今天上午8点提醒我检查事情A",
        now=datetime(2026, 5, 22, 9, 0),
    )

    assert parsed is None
