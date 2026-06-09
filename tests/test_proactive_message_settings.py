"""Phase 1 主动消息设定：DB helper + settings 模块 + policy 集成。"""
from datetime import datetime

import pytest


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _evaluate(account_id: str, category: str, now: datetime):
    from app.proactive.policy import OutboundCategory, evaluate_outbound_policy

    return evaluate_outbound_policy(
        account_id=account_id,
        category=OutboundCategory(category),
        source="test",
        scheduled_at=None,
        now=now,
    )


# 非静默、非临时静默的安全时间点
DAYTIME = datetime(2026, 5, 30, 10, 0)


def test_effective_defaults_when_no_row(fresh_db):
    from app.proactive.settings import get_effective_proactive_message_settings

    eff = get_effective_proactive_message_settings("nobody")
    assert eff["master_enabled"] is True
    assert eff["quiet_hours"] == {"enabled": True, "start": "22:00", "end": "08:00"}
    assert eff["quiet_hours_is_user"] is False
    assert all(c["enabled"] for c in eff["categories"].values())
    assert eff["muted_until"] is None


def test_sparse_merge_master_only_keeps_global_quiet_hours(fresh_db):
    """只设 master_enabled 后，quiet_hours 仍跟随全局（P0-1 稀疏存储）。"""
    from app.proactive.settings import (
        apply_proactive_message_settings_patch,
        get_effective_proactive_message_settings,
    )

    _create_account("acc-sparse")
    apply_proactive_message_settings_patch(
        account_id="acc-sparse",
        patch={"master_enabled": True},
        source="tool",
    )
    # 改全局静默时段；账号没有显式设置 quiet_hours，应跟随新全局值
    fresh_db.proactive_quiet_hours_start = "23:30"
    fresh_db.proactive_quiet_hours_end = "06:30"
    eff = get_effective_proactive_message_settings("acc-sparse")
    assert eff["quiet_hours"] == {"enabled": True, "start": "23:30", "end": "06:30"}
    assert eff["quiet_hours_is_user"] is False


def test_master_disabled_blocks_proactive_but_not_reminder(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-master")
    apply_proactive_message_settings_patch(
        account_id="acc-master",
        patch={"master_enabled": False},
        source="tool",
    )
    for cat in (
        "companion_followup",
        "content_invitation",
        "reactivation_topic_followup",
        "reactivation_content_invitation",
        "legacy_proactive",
    ):
        decision = _evaluate("acc-master", cat, DAYTIME)
        assert decision.allowed is False, cat
        assert decision.reason == "proactive_master_disabled", cat

    # 提醒豁免：仍允许
    reminder = _evaluate("acc-master", "user_reminder", DAYTIME)
    assert reminder.allowed is True
    assert reminder.reason is None


def test_category_disable_is_scoped(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-cat")
    apply_proactive_message_settings_patch(
        account_id="acc-cat",
        patch={"category_updates": {"content_invitation": {"enabled": False}}},
        source="tool",
    )
    blocked = _evaluate("acc-cat", "content_invitation", DAYTIME)
    assert blocked.allowed is False
    assert blocked.reason == "proactive_category_disabled"

    allowed = _evaluate("acc-cat", "companion_followup", DAYTIME)
    assert allowed.allowed is True


def test_muted_until_blocks_then_clears(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-mute")
    apply_proactive_message_settings_patch(
        account_id="acc-mute",
        patch={"muted_until": "2099-01-01 00:00:00"},
        source="tool",
    )
    blocked = _evaluate("acc-mute", "companion_followup", DAYTIME)
    assert blocked.allowed is False
    assert blocked.reason == "proactive_muted"
    assert blocked.next_allowed_at == "2099-01-01 00:00:00"

    # 传过去时间 → 清除临时静默
    apply_proactive_message_settings_patch(
        account_id="acc-mute",
        patch={"muted_until": "2000-01-01 00:00:00"},
        source="tool",
    )
    cleared = _evaluate("acc-mute", "companion_followup", DAYTIME)
    assert cleared.allowed is True


def test_user_quiet_hours_override_global(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-quiet")
    # 用户设 23:00-07:00，覆盖全局 22:00-08:00
    apply_proactive_message_settings_patch(
        account_id="acc-quiet",
        patch={"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"}},
        source="tool",
    )
    # 22:30 在全局静默内、但不在用户静默内 → 放行（证明 override 替换而非叠加）
    at_2230 = _evaluate("acc-quiet", "companion_followup", datetime(2026, 5, 30, 22, 30))
    assert at_2230.allowed is True

    # 23:30 在用户静默内 → 拦截，专属 reason
    at_2330 = _evaluate("acc-quiet", "companion_followup", datetime(2026, 5, 30, 23, 30))
    assert at_2330.allowed is False
    assert at_2330.reason == "proactive_user_quiet_hours"


def test_user_quiet_hours_disabled_allows_night(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-noquiet")
    apply_proactive_message_settings_patch(
        account_id="acc-noquiet",
        patch={"quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"}},
        source="tool",
    )
    at_night = _evaluate("acc-noquiet", "companion_followup", datetime(2026, 5, 30, 23, 30))
    assert at_night.allowed is True


def test_account_isolation(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-A")
    _create_account("acc-B")
    apply_proactive_message_settings_patch(
        account_id="acc-A",
        patch={"master_enabled": False},
        source="tool",
    )
    assert _evaluate("acc-A", "companion_followup", DAYTIME).allowed is False
    assert _evaluate("acc-B", "companion_followup", DAYTIME).allowed is True


def test_update_writes_audit_event(fresh_db):
    from app.db import create_tool_invocation, list_proactive_message_setting_events
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-audit")
    invocation = create_tool_invocation(
        account_id="acc-audit",
        tool_name="update_proactive_message_settings",
        args={"master_enabled": False},
    )
    apply_proactive_message_settings_patch(
        account_id="acc-audit",
        patch={"master_enabled": False},
        source="tool",
        tool_invocation_id=invocation["id"],
        reason="user_requested",
    )
    events = list_proactive_message_setting_events(account_id="acc-audit")
    assert len(events) == 1
    assert events[0]["source"] == "tool"
    assert events[0]["tool_invocation_id"] == invocation["id"]
    assert events[0]["reason"] == "user_requested"


def test_invalid_quiet_hours_rejected(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-bad")
    with pytest.raises(ValueError):
        apply_proactive_message_settings_patch(
            account_id="acc-bad",
            patch={"quiet_hours": {"start": "25:00", "end": "08:00"}},
            source="tool",
        )


def test_unknown_category_rejected(fresh_db):
    from app.proactive.settings import apply_proactive_message_settings_patch

    _create_account("acc-badcat")
    with pytest.raises(ValueError):
        apply_proactive_message_settings_patch(
            account_id="acc-badcat",
            patch={"category_updates": {"user_reminder": {"enabled": False}}},
            source="tool",
        )
