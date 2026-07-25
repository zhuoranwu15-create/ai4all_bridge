"""M2-C C5：真人级主动触达防 N× 门禁。"""
from datetime import datetime

import app.db as db
from app.products.zhaoxi.application import (
    HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON,
    human_level_proactive_allowed,
)
from app.time_utils import beijing_naive_now
from tests.factories import create_account, create_route, make_resident_account


def _resident_pair(phone: str, *, legacy_primary: bool) -> tuple[str, str, str]:
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name="主动门禁用户"
    )["id"]
    account_a = make_resident_account(user_id, "主动居民A")
    account_b = make_resident_account(user_id, "主动居民B")
    scope = db.resolve_resident_proactive_scope(runtime_account_id=account_a)
    universe_id = str(scope["universe_id"])
    if legacy_primary:
        db.mark_universe_legacy_confirmed(
            universe_id=universe_id,
            legacy_primary_account_id=account_a,
        )
    return universe_id, account_a, account_b


def test_gate_allows_form_a_and_only_legacy_primary_resident(fresh_db):
    form_a = "acc-proactive-form-a"
    create_account(form_a)
    _, primary, secondary = _resident_pair(
        "19950004001", legacy_primary=True
    )
    create_route(primary)
    _, app_a, app_b = _resident_pair("19950004002", legacy_primary=False)

    assert human_level_proactive_allowed(form_a) is True
    assert human_level_proactive_allowed(primary) is True
    assert human_level_proactive_allowed(secondary) is False
    assert human_level_proactive_allowed(app_a) is False
    assert human_level_proactive_allowed(app_b) is False


def test_safety_switch_can_restore_legacy_behavior(fresh_db):
    _, _primary, secondary = _resident_pair(
        "19950004009", legacy_primary=True
    )
    fresh_db.companion_world_proactive_safety_enabled = False
    assert human_level_proactive_allowed(secondary) is True


def test_real_weixin_primary_wins_even_when_app_only_flags_are_on(fresh_db):
    _, primary, secondary = _resident_pair(
        "19950004010", legacy_primary=True
    )
    create_route(primary)
    fresh_db.companion_world_app_inbox_enabled = True
    fresh_db.companion_world_app_only_human_proactive_enabled = True

    assert human_level_proactive_allowed(primary) is True
    assert human_level_proactive_allowed(secondary) is False
    from app.products.zhaoxi.proactive.contract.common import _select_route

    route = _select_route(primary)
    assert route is not None and route["channel"] == "openclaw-weixin"


def test_planning_blocks_before_human_level_generators(fresh_db):
    from app.products.zhaoxi.proactive.orchestration.planning import (
        plan_new_user_reactivation_candidate,
        plan_reactivation_candidate,
    )

    _, _primary, secondary = _resident_pair(
        "19950004003", legacy_primary=True
    )
    calls = []

    def forbidden_generator(**kwargs):
        calls.append(kwargs)
        raise AssertionError("blocked resident must not burn generator/LLM work")

    planned = plan_reactivation_candidate(
        account_id=secondary,
        now=datetime(2026, 7, 22, 10, 0),
        topic_followup_generator=forbidden_generator,
        content_invitation_generator=forbidden_generator,
        hot_topic_generator=forbidden_generator,
    )
    new_user = plan_new_user_reactivation_candidate(
        account_id=secondary,
        now=datetime(2026, 7, 22, 10, 0),
        topic_followup_generator=forbidden_generator,
        hot_topic_generator=forbidden_generator,
    )

    assert calls == []
    assert planned["reason"] == HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON
    assert new_user["reason"] == HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON


def test_reactivation_delivery_blocks_old_candidate_and_clears_it(fresh_db):
    from app.products.zhaoxi.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.products.zhaoxi.proactive.store.account_state import ensure_account_state
    from app.products.zhaoxi.proactive.store.candidates import (
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )

    _, _primary, secondary = _resident_pair(
        "19950004004", legacy_primary=True
    )
    ensure_account_state(account_id=secondary)
    upsert_reactivation_candidate(
        account_id=secondary,
        candidate={
            "id": "blocked-reactivation",
            "type": "topic_followup",
            "text": "不应发送",
            "scheduled_at": "2026-07-22 10:00:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id=secondary,
        now=datetime(2026, 7, 22, 10, 0),
        dry_run=False,
    )

    assert result["action"] == "no_op"
    assert result["reason"] == HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON
    assert get_reactivation_candidate(account_id=secondary) is None


def test_account_check_has_planning_and_delivery_defense(fresh_db, monkeypatch):
    from app.products.zhaoxi.proactive.delivery.account_check import (
        decide_account_check_action,
        execute_account_check_decision,
    )
    from app.products.zhaoxi.proactive.store.account_state import ensure_account_state

    _, _primary, secondary = _resident_pair(
        "19950004005", legacy_primary=True
    )
    create_route(secondary)
    ensure_account_state(
        account_id=secondary,
        enabled=True,
        metadata={
            "account_check_candidate": {
                "id": "blocked-account-check",
                "text": "不应发送",
                "source": "test",
            }
        },
    )
    decision = decide_account_check_action(
        account_id=secondary,
        now=beijing_naive_now(),
    )
    assert decision["action"] == "no_op"
    assert decision["reason"] == HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON

    monkeypatch.setattr(
        "app.products.zhaoxi.proactive.delivery.account_check.dispatch_proactive_text",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("delivery defense must run before outbound")
        ),
    )
    execution = execute_account_check_decision(
        decision={
            "action": "send_text",
            "account_id": secondary,
            "source": "account_check",
            "text": "旧决策也不能发送",
            "route": {
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "to_user_id": "user@im.wechat",
                "session_key": f"session-{secondary}",
            },
        },
        now=beijing_naive_now(),
    )
    assert execution["status"] == "skipped"
    assert execution["reason"] == HUMAN_LEVEL_PROACTIVE_BLOCKED_REASON


def test_per_resident_reminder_and_commitment_are_not_gated(fresh_db, monkeypatch):
    from app.products.zhaoxi.proactive.obligations.commitments import dispatch_commitment
    from app.products.zhaoxi.proactive.obligations.reminders import dispatch_reminder
    from app.products.zhaoxi.proactive.store.account_state import ensure_account_state

    _, _primary, secondary = _resident_pair(
        "19950004006", legacy_primary=True
    )
    create_route(secondary)
    now = beijing_naive_now().replace(microsecond=0)
    due_at = now.strftime("%Y-%m-%d %H:%M:%S")
    ensure_account_state(account_id=secondary, enabled=True)
    db.create_reminder(
        reminder_id="resident-reminder",
        account_id=secondary,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{secondary}",
        text="居民自己的提醒",
        due_at=due_at,
    )
    db.create_proactive_commitment(
        commitment_id="resident-commitment",
        dedupe_key="resident-commitment",
        account_id=secondary,
        text="居民自己的承诺",
        due_at=due_at,
    )
    reminder_calls = []
    commitment_calls = []
    monkeypatch.setattr(
        "app.products.zhaoxi.proactive.obligations.reminders.dispatch_proactive_text",
        lambda **kwargs: reminder_calls.append(kwargs) or {"status": "sent"},
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.proactive.obligations.commitments.dispatch_proactive_text",
        lambda **kwargs: commitment_calls.append(kwargs) or {"status": "sent"},
    )

    reminder = dispatch_reminder(reminder_id="resident-reminder", now=now)
    commitment = dispatch_commitment(
        commitment_id="resident-commitment", now=now
    )

    assert reminder["status"] == "sent" and len(reminder_calls) == 1
    assert commitment["status"] == "sent" and len(commitment_calls) == 1
    assert reminder_calls[0]["source"] == "reminder"
    assert commitment_calls[0]["source"] == "commitment"
