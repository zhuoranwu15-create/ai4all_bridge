"""Phase 2 主动消息：允许推送时段窗口（policy 门禁 + scheduler 对齐）。"""
from datetime import datetime

import pytest

from app.products.zhaoxi.proactive.preferences import WEEKDAYS


from tests.factories import create_account as _create_account


def _evaluate(account_id, category, now):
    from app.products.zhaoxi.proactive.delivery.policy import OutboundCategory, evaluate_outbound_policy

    return evaluate_outbound_policy(
        account_id=account_id,
        category=OutboundCategory(category),
        source="test",
        scheduled_at=None,
        now=now,
    )


# 2026-05-29 是周五；用 weekday() 推导标签，避免硬编码出错
FRIDAY_10 = datetime(2026, 5, 29, 10, 0)
FRIDAY_13 = datetime(2026, 5, 29, 13, 0)
FRIDAY_LABEL = WEEKDAYS[FRIDAY_10.weekday()]
SATURDAY_LABEL = WEEKDAYS[(FRIDAY_10.weekday() + 1) % 7]


def test_inside_window_allows(fresh_db):
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-win1")
    apply_proactive_message_settings_patch(
        account_id="acc-win1",
        patch={"allowed_windows": [{"days": [FRIDAY_LABEL], "start": "09:00", "end": "12:00"}]},
        source="tool",
    )
    decision = _evaluate("acc-win1", "companion_followup", FRIDAY_10)
    assert decision.allowed is True


def test_outside_window_time_blocks(fresh_db):
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-win2")
    apply_proactive_message_settings_patch(
        account_id="acc-win2",
        patch={"allowed_windows": [{"days": [FRIDAY_LABEL], "start": "09:00", "end": "12:00"}]},
        source="tool",
    )
    decision = _evaluate("acc-win2", "companion_followup", FRIDAY_13)
    assert decision.allowed is False
    assert decision.reason == "proactive_outside_allowed_window"


def test_outside_window_day_blocks(fresh_db):
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-win3")
    apply_proactive_message_settings_patch(
        account_id="acc-win3",
        patch={"allowed_windows": [{"days": [SATURDAY_LABEL], "start": "09:00", "end": "12:00"}]},
        source="tool",
    )
    decision = _evaluate("acc-win3", "companion_followup", FRIDAY_10)  # 周五，窗口只允许周六
    assert decision.allowed is False
    assert decision.reason == "proactive_outside_allowed_window"


def test_empty_windows_no_constraint(fresh_db):
    _create_account("acc-win4")
    decision = _evaluate("acc-win4", "companion_followup", FRIDAY_13)
    assert decision.allowed is True  # 未设窗口 = 不限制


def test_cross_midnight_window_rejected(fresh_db):
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-win5")
    with pytest.raises(ValueError):
        apply_proactive_message_settings_patch(
            account_id="acc-win5",
            patch={"allowed_windows": [{"days": ["FRI"], "start": "22:00", "end": "02:00"}]},
            source="tool",
        )


def test_invalid_weekday_rejected(fresh_db):
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-win6")
    with pytest.raises(ValueError):
        apply_proactive_message_settings_patch(
            account_id="acc-win6",
            patch={"allowed_windows": [{"days": ["FUN"], "start": "09:00", "end": "12:00"}]},
            source="tool",
        )


def test_scheduler_snaps_to_window_start(fresh_db):
    """固定 slots(12:15/18:15/21:05)都不在 13:00-14:00 内 → snap 到窗口起点。"""
    from app.products.zhaoxi.proactive.slots import next_reactivation_slot

    windows = [{"days": [FRIDAY_LABEL], "start": "13:00", "end": "14:00"}]
    result = next_reactivation_slot(now=FRIDAY_10, allowed_windows=windows)
    assert result["scheduled_slot"] == "window_start"
    assert result["scheduled_at"].endswith("13:00:00")


def test_scheduler_picks_slot_inside_window(fresh_db):
    """窗口 12:00-13:00 含固定 slot 12:15 → 直接用该 slot。"""
    from app.products.zhaoxi.proactive.slots import next_reactivation_slot

    windows = [{"days": [FRIDAY_LABEL], "start": "12:00", "end": "13:00"}]
    result = next_reactivation_slot(now=FRIDAY_10, allowed_windows=windows)
    assert result["scheduled_at"].endswith("12:15:00")


def test_scheduler_no_windows_unchanged(fresh_db):
    """无窗口时行为不变：取当天首个 >=now 的 slot。"""
    from app.products.zhaoxi.proactive.slots import next_reactivation_slot

    result = next_reactivation_slot(now=FRIDAY_10, allowed_windows=[])
    assert result["scheduled_at"].endswith("12:15:00")
