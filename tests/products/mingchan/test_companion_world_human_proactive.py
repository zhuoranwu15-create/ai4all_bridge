"""M3-5 真人级 proactive：owner 聚合、speaker、App-only claim/CAS。"""
from datetime import datetime

import app.db as db
from app.bootstrap.product_registry import MINGCHAN_APP_ID, build_test_product_registry
from app.products.mingchan.application import human_level_proactive_allowed
from app.products.mingchan.infrastructure.app_inbox import AppInboxAdapter, HumanAppInboxIntent
from tests.factories import make_resident_account


def _world_with_two_residents(phone: str):
    user_id = db.create_or_get_platform_user_by_phone(
        phone=phone, display_name="真人级主动触达用户"
    )["id"]
    account_a = make_resident_account(user_id, "居民甲", app_id=MINGCHAN_APP_ID)
    account_b = make_resident_account(user_id, "居民乙", app_id=MINGCHAN_APP_ID)
    scope = db.resolve_resident_memory_scope(runtime_account_id=account_a)
    for account_id in (account_a, account_b):
        resident_scope = db.resolve_resident_memory_scope(
            runtime_account_id=account_id
        )
        db.create_ai_conversation(
            universe_id=resident_scope["universe_id"],
            resident_id=resident_scope["resident_id"],
            owner_platform_user_id=user_id,
            runtime_account_id=account_id,
        )
    db.set_universe_onboarding_state(
        universe_id=scope["universe_id"], onboarding_state="confirmed"
    )
    return user_id, str(scope["universe_id"]), account_a, account_b


def _insert_app_message(account_id: str, *, message_id: str, role: str, at: str):
    session = db.get_or_create_session(
        account_id=account_id,
        channel="native",
        sender_id="owner",
        sender_name=None,
        chat_id=None,
        session_key="__app_active__",
    )["session"]
    inserted = db.insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=message_id,
        reply_to_message_id=None,
        direction="inbound" if role == "user" else "outbound",
        role=role,
        message_type="text",
        content=message_id,
    )
    with db.connect() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", (at, inserted))


def test_app_speaker_prefers_resident_inbound_then_app_activity(fresh_db):
    user_id, universe_id, account_a, account_b = _world_with_two_residents(
        "19966001001"
    )
    _insert_app_message(
        account_a, message_id="a-user", role="user", at="2026-07-22 09:00:00"
    )
    _insert_app_message(
        account_b,
        message_id="b-assistant",
        role="assistant",
        at="2026-07-22 10:00:00",
    )

    first = db.select_human_app_speaker(
        platform_user_id=user_id, universe_id=universe_id
    )
    assert first["runtime_account_id"] == account_a

    _insert_app_message(
        account_b, message_id="b-user", role="user", at="2026-07-22 11:00:00"
    )
    second = db.select_human_app_speaker(
        platform_user_id=user_id, universe_id=universe_id
    )
    assert second["runtime_account_id"] == account_b
    assert db.get_owner_last_inbound_at(platform_user_id=user_id) == (
        "2026-07-22 11:00:00"
    )


def test_owner_activity_aggregation_excludes_other_product_bindings(fresh_db):
    registry = build_test_product_registry()
    user_id, universe_id, resident_a, resident_b = _world_with_two_residents(
        "19966001004"
    )
    zhaoxi_id = db.create_ai4all_account_for_user(
        app_id="zhaoxi",
        platform_user_id=user_id, display_name="朝夕渠道角色"
    )["account"]["id"]
    db.ensure_product_membership(
        platform_user_id=user_id, app_id="test_product", registry=registry
    )
    other_id = db.create_ai4all_account_for_user(
        platform_user_id=user_id,
        display_name="其他产品角色",
        app_id="test_product",
        registry=registry,
    )["account"]["id"]
    _insert_app_message(
        other_id,
        message_id="other-product-inbound",
        role="user",
        at="2026-07-22 11:00:00",
    )

    assert set(db.list_human_proactive_account_ids_for_user(platform_user_id=user_id)) == {
        resident_a,
        resident_b,
    }
    assert db.get_owner_last_inbound_at(platform_user_id=user_id) is None
    assert db.count_owner_inbound_after(
        platform_user_id=user_id, after="2026-07-22 00:00:00"
    ) == 0
    assert db.list_ai_feed_eligible_worlds(
        inbound_since="2026-07-15 00:00:00"
    ) == []

    _insert_app_message(
        zhaoxi_id,
        message_id="zhaoxi-inbound",
        role="user",
        at="2026-07-22 12:00:00",
    )
    assert db.get_owner_last_inbound_at(platform_user_id=user_id) is None
    assert db.count_owner_inbound_after(
        platform_user_id=user_id, after="2026-07-22 00:00:00"
    ) == 0
    _insert_app_message(
        resident_a,
        message_id="mingchan-inbound",
        role="user",
        at="2026-07-22 12:30:00",
    )
    assert db.get_owner_last_inbound_at(platform_user_id=user_id) == (
        "2026-07-22 12:30:00"
    )
    assert db.count_owner_inbound_after(
        platform_user_id=user_id, after="2026-07-22 00:00:00"
    ) == 1
    assert [
        row["universe_id"]
        for row in db.list_ai_feed_eligible_worlds(
            inbound_since="2026-07-15 00:00:00"
        )
    ] == [universe_id]


def test_old_reservation_token_cannot_finalize_or_cancel_reacquired_lease(fresh_db):
    user_id, universe_id, _account_a, _account_b = _world_with_two_residents(
        "19966001003"
    )
    speaker = db.select_human_app_speaker(
        platform_user_id=user_id, universe_id=universe_id
    )
    kwargs = dict(
        platform_user_id=user_id,
        universe_id=universe_id,
        category="content_invitation",
        source_type="reactivation",
        source_id="due-token",
        idempotency_key="human-proactive:v1:content_invitation:token",
        request_fingerprint="fingerprint-token",
    )
    reserved, acquired = db.reserve_human_app_notification(
        **kwargs,
        claim_token="old-token",
        claim_expires_at="2026-07-22 10:05:00",
        now="2026-07-22 10:00:00",
        visible_since="2026-07-21 10:00:00",
    )
    reacquired, reacquired_ok = db.reserve_human_app_notification(
        **kwargs,
        claim_token="new-token",
        claim_expires_at="2026-07-22 10:11:00",
        now="2026-07-22 10:06:00",
        visible_since="2026-07-21 10:06:00",
    )
    assert acquired is True and reacquired_ok is True
    assert reacquired["id"] == reserved["id"]

    _row, old_cancelled = db.cancel_human_app_notification(
        notification_id=reserved["id"],
        platform_user_id=user_id,
        claim_token="old-token",
        reason="old-worker",
        now="2026-07-22 10:06:01",
        expires_at="2026-07-29 10:06:01",
    )
    _row, old_finalized = db.finalize_human_app_notification(
        notification_id=reserved["id"],
        platform_user_id=user_id,
        universe_id=universe_id,
        claim_token="old-token",
        expected_resident_id=speaker["resident_id"],
        allow_speaker_reselection=True,
        title=None,
        body_text="旧 worker",
        target_type="conversation",
        target_id=speaker["conversation_id"],
        delivered_at="2026-07-22 10:06:01",
        expires_at="2026-08-21 10:06:01",
        now="2026-07-22 10:06:01",
    )
    assert old_cancelled is False and old_finalized is False

    current, finalized = db.finalize_human_app_notification(
        notification_id=reserved["id"],
        platform_user_id=user_id,
        universe_id=universe_id,
        claim_token="new-token",
        expected_resident_id=speaker["resident_id"],
        allow_speaker_reselection=True,
        title=None,
        body_text="新 worker",
        target_type="conversation",
        target_id=speaker["conversation_id"],
        delivered_at="2026-07-22 10:06:02",
        expires_at="2026-08-21 10:06:02",
        now="2026-07-22 10:06:02",
    )
    assert finalized is True and current["delivery_status"] == "visible"

    blocked, blocked_acquired = db.reserve_human_app_notification(
        **{**kwargs, "source_id": "before-boundary", "idempotency_key": "human-proactive:v1:content_invitation:before-boundary"},
        claim_token="before-boundary",
        claim_expires_at="2026-07-23 10:10:00",
        now="2026-07-23 10:06:01",
        visible_since="2026-07-22 10:06:01",
    )
    assert blocked is None and blocked_acquired is False
    boundary, boundary_acquired = db.reserve_human_app_notification(
        **{**kwargs, "source_id": "at-boundary", "idempotency_key": "human-proactive:v1:content_invitation:at-boundary"},
        claim_token="at-boundary",
        claim_expires_at="2026-07-23 10:11:02",
        now="2026-07-23 10:06:02",
        visible_since="2026-07-22 10:06:02",
    )
    assert boundary_acquired is True and boundary["delivery_status"] == "reserved"


def test_finalize_reselects_generic_but_cancels_speaker_bound_content(fresh_db):
    user_id, universe_id, account_a, account_b = _world_with_two_residents(
        "19966001004"
    )
    fresh_db.mingchan_app_inbox_enabled = True
    fresh_db.mingchan_app_only_human_proactive_enabled = True
    initial = db.select_human_app_speaker(
        platform_user_id=user_id, universe_id=universe_id
    )
    other_account = account_a if initial["runtime_account_id"] == account_b else account_b

    generic = HumanAppInboxIntent(
        runtime_account_id=initial["runtime_account_id"],
        category="companion_followup",
        source_type="account_check",
        source_dedupe_key="generic-reselect",
        body_text="通用问候",
    )
    claim, acquired = AppInboxAdapter().reserve_human(
        generic, now=datetime(2026, 7, 22, 14, 0, 0)
    )
    assert acquired is True and claim is not None
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET status='offline' WHERE id=?",
            (initial["resident_id"],),
        )
    notification, finalized = AppInboxAdapter().finalize_human(
        claim, generic, now=datetime(2026, 7, 22, 14, 0, 1)
    )
    assert finalized is True
    final_scope = db.resolve_resident_memory_scope(runtime_account_id=other_account)
    assert notification.resident_id == final_scope["resident_id"]

    # 恢复原 speaker 并让其重新成为 hint，再验证强绑定内容在 hint 失活时取消。
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET status='active' WHERE id=?",
            (initial["resident_id"],),
        )
        conn.execute(
            "UPDATE app_notifications SET delivered_at='2026-07-21 13:59:59' WHERE id=?",
            (claim.notification_id,),
        )
    bound = HumanAppInboxIntent(
        runtime_account_id=initial["runtime_account_id"],
        category="content_invitation",
        source_type="reactivation",
        source_dedupe_key="bound-cancel",
        body_text="绑定原居民的内容",
        speaker_bound=True,
    )
    bound_claim, bound_acquired = AppInboxAdapter().reserve_human(
        bound, now=datetime(2026, 7, 22, 14, 0, 2)
    )
    assert bound_acquired is True and bound_claim is not None
    with db.connect() as conn:
        conn.execute(
            "UPDATE universe_residents SET status='offline' WHERE id=?",
            (bound_claim.expected_resident_id,),
        )
    bound_notification, bound_finalized = AppInboxAdapter().finalize_human(
        bound_claim, bound, now=datetime(2026, 7, 22, 14, 0, 3)
    )
    assert bound_finalized is False
    assert bound_notification.delivery_status == "cancelled"
