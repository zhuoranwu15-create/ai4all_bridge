from datetime import datetime
from unittest.mock import patch


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


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def _insert_inbound(account_id: str, *, created_at: str) -> None:
    from app.db import connect, insert_message, list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id, limit=1)[0]
    row_id = insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"inbound-{account_id}-{created_at}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="刚刚又聊了一句",
        raw={"source": "test"},
    )
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )


def test_reactivation_candidate_upsert_replace_and_clear_preserves_metadata(fresh_db):
    from app.db import get_proactive_account_state, upsert_proactive_account_state
    from app.proactive.reactivation import (
        REACTIVATION_METADATA_KEY,
        clear_reactivation_candidate,
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )

    _create_account("acc-reactivation")
    upsert_proactive_account_state(
        account_id="acc-reactivation",
        metadata={"existing": "keep"},
    )

    first_state = upsert_reactivation_candidate(
        account_id="acc-reactivation",
        candidate={
            "id": "react-1",
            "type": "topic_followup",
            "topic": "相亲聊天压力",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "reason": "用户昨天反复讨论相亲回复压力",
            "confidence": 0.9,
            "generated_at": "2026-06-05 10:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "source_message_cutoff_id": "123",
            "dedupe": {"checked": True, "duplicate": False},
        },
    )
    second_state = upsert_reactivation_candidate(
        account_id="acc-reactivation",
        candidate={
            "id": "react-2",
            "type": "topic_followup",
            "text": "昨晚小家伙睡得乖不乖？",
        },
    )
    candidate = get_reactivation_candidate(account_id="acc-reactivation")
    cleared = clear_reactivation_candidate(
        account_id="acc-reactivation",
        reason="sent",
        now=datetime(2026, 6, 5, 12, 15),
    )

    assert first_state["metadata"]["existing"] == "keep"
    assert first_state["metadata"][REACTIVATION_METADATA_KEY]["source_message_cutoff_id"] == 123
    assert second_state["metadata"][REACTIVATION_METADATA_KEY]["id"] == "react-2"
    assert candidate["text"] == "昨晚小家伙睡得乖不乖？"
    assert REACTIVATION_METADATA_KEY not in cleared["metadata"]
    assert cleared["metadata"]["existing"] == "keep"
    assert cleared["metadata"]["reactivation_candidate_cleared_reason"] == "sent"
    assert get_proactive_account_state(account_id="acc-reactivation")["metadata"]["existing"] == "keep"


def test_content_invitation_reactivation_requires_invitation_id():
    from app.proactive.reactivation import normalize_reactivation_candidate

    try:
        normalize_reactivation_candidate(
            {
                "id": "react-content",
                "type": "content_invitation",
                "text": "要不要看几条中亚五国相关内容？",
            }
        )
    except ValueError as exc:
        assert "content_invitation_id" in str(exc)
    else:
        raise AssertionError("content_invitation candidate without id should fail")


def test_reactivation_outbound_metadata_includes_dedupe_and_type():
    from app.proactive.reactivation import reactivation_outbound_metadata

    metadata = reactivation_outbound_metadata(
        candidate={
            "id": "react-3",
            "type": "content_invitation",
            "content_invitation_id": "cinv-1",
            "topic": "中亚五国轻知识",
            "text": "Mark，要不要看几条中亚五国相关内容？",
            "generated_at": "2026-06-05 10:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "dedupe": {"checked": True, "duplicate": False},
        },
        sent_at=datetime(2026, 6, 5, 12, 15, 3),
        extra={"policy": {"daily_limit_key": "reactivation"}},
    )

    assert metadata["reactivation"] is True
    assert metadata["reactivation_type"] == "content_invitation"
    assert metadata["content_invitation_id"] == "cinv-1"
    assert metadata["dedupe"]["duplicate"] is False
    assert metadata["sent_at"] == "2026-06-05 12:15:03"
    assert metadata["policy"]["daily_limit_key"] == "reactivation"


def test_dispatch_reactivation_dry_run_would_send_without_outbound(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-dispatch")
    _create_route("acc-react-dispatch")
    ensure_account_state(account_id="acc-react-dispatch")
    upsert_reactivation_candidate(
        account_id="acc-react-dispatch",
        candidate={
            "id": "react-send-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "dedupe": {"checked": True, "duplicate": False},
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-dispatch",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    assert result["action"] == "would_send"
    assert result["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert result["outbound_metadata"]["reactivation"] is True
    assert result["outbound_metadata"]["dry_run"] is True
    assert list_outbound_messages(account_id="acc-react-dispatch") == []


def test_dispatch_reactivation_real_send_uses_reactivation_category_for_quota_and_dedupe(fresh_db):
    from app.db import (
        count_reactivation_outbound_for_quota_date,
        list_outbound_messages,
        list_recent_reactivation_outbound_messages,
    )
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-real")
    _create_route("acc-react-real")
    ensure_account_state(account_id="acc-react-real")
    upsert_reactivation_candidate(
        account_id="acc-react-real",
        candidate={
            "id": "react-real-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    with patch(
        "app.proactive.messaging.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:react-real-1"},
    ):
        first = dispatch_reactivation_candidate(
            account_id="acc-react-real",
            now=datetime(2026, 6, 5, 12, 15),
            dry_run=False,
            dedupe_checker=lambda **kwargs: {
                "checked": True,
                "duplicate": False,
                "reason": "test",
            },
        )

    outbound = list_outbound_messages(account_id="acc-react-real", limit=10)
    recent_history = list_recent_reactivation_outbound_messages(
        account_id="acc-react-real",
        since="2000-01-01 00:00:00",
    )

    assert first["action"] == "sent"
    assert first["outbound_message"]["product_category"] == "reactivation_topic_followup"
    assert outbound[0]["product_category"] == "reactivation_topic_followup"
    assert count_reactivation_outbound_for_quota_date(
        account_id="acc-react-real",
        quota_date="2026-06-05",
    ) == 1
    assert [item["id"] for item in recent_history] == [outbound[0]["id"]]
    assert get_reactivation_candidate(account_id="acc-react-real") is None

    upsert_reactivation_candidate(
        account_id="acc-react-real",
        candidate={
            "id": "react-real-2",
            "type": "content_invitation",
            "text": "要不要看看几条中亚五国内容？",
            "content_invitation_id": "cinv-react-real-2",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )
    second = dispatch_reactivation_candidate(
        account_id="acc-react-real",
        now=datetime(2026, 6, 5, 18, 15),
        dry_run=False,
    )

    assert second["action"] == "no_op"
    assert second["reason"] == "reactivation_daily_limit_already_sent"
    assert count_reactivation_outbound_for_quota_date(
        account_id="acc-react-real",
        quota_date="2026-06-05",
    ) == 1


def test_dispatch_reactivation_content_invitation_marks_row_invited(fresh_db):
    from app.db import (
        create_content_invitation,
        get_content_invitation,
        list_outbound_messages,
    )
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-ci")
    _create_route("acc-react-ci")
    ensure_account_state(account_id="acc-react-ci")
    invitation = create_content_invitation(
        account_id="acc-react-ci",
        topic="中亚五国",
        invitation_text="要不要看看几条中亚五国的内容？",
        title_items=[{"title": "标题一"}, {"title": "标题二"}, {"title": "标题三"}],
        expires_at="2099-01-01 00:00:00",
    )
    upsert_reactivation_candidate(
        account_id="acc-react-ci",
        candidate={
            "id": "react-ci-1",
            "type": "content_invitation",
            "text": "要不要看看几条中亚五国的内容？",
            "content_invitation_id": invitation["id"],
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    with patch(
        "app.proactive.messaging.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:react-ci-1"},
    ):
        result = dispatch_reactivation_candidate(
            account_id="acc-react-ci",
            now=datetime(2026, 6, 5, 18, 15),
            dry_run=False,
            dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
        )

    outbound = list_outbound_messages(account_id="acc-react-ci", limit=10)
    updated = get_content_invitation(invitation_id=invitation["id"])

    assert result["action"] == "sent"
    assert outbound[0]["product_category"] == "reactivation_content_invitation"
    assert outbound[0]["metadata"]["reactivation"] is True
    # Reactivation owns advancing the content_invitations state machine so the
    # downstream "send titles" interaction stays available.
    assert updated["status"] == "invited"
    assert updated["outbound_message_id"] == outbound[0]["id"]
    assert get_reactivation_candidate(account_id="acc-react-ci") is None


def test_dispatch_reactivation_content_invitation_missing_row_clears_candidate(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-ci-missing")
    _create_route("acc-react-ci-missing")
    ensure_account_state(account_id="acc-react-ci-missing")
    upsert_reactivation_candidate(
        account_id="acc-react-ci-missing",
        candidate={
            "id": "react-ci-missing",
            "type": "content_invitation",
            "text": "要不要看看几条内容？",
            "content_invitation_id": "cinv-does-not-exist",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-ci-missing",
        now=datetime(2026, 6, 5, 18, 15),
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    assert result["action"] == "no_op"
    assert result["reason"] == "content_invitation_not_claimable"
    assert list_outbound_messages(account_id="acc-react-ci-missing") == []
    assert get_reactivation_candidate(account_id="acc-react-ci-missing") is None


def test_dispatch_reactivation_recent_inbound_reschedules_to_next_slot(fresh_db):
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-delay")
    _create_route("acc-react-delay")
    ensure_account_state(account_id="acc-react-delay")
    _insert_inbound("acc-react-delay", created_at="2026-06-05 11:45:00")
    upsert_reactivation_candidate(
        account_id="acc-react-delay",
        candidate={
            "id": "react-delay-1",
            "type": "topic_followup",
            "text": "昨晚小家伙睡得乖不乖？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-delay",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
    )
    candidate = get_reactivation_candidate(account_id="acc-react-delay")

    assert result["action"] == "delayed"
    assert result["reason"] == "recent_inbound"
    assert candidate["scheduled_slot"] == "slot_2"
    assert candidate["scheduled_at"] == "2026-06-05 18:15:00"
    assert candidate["policy"]["last_reschedule_reason"] == "recent_inbound"


def test_dispatch_reactivation_dedupe_regenerates_once_in_dry_run(fresh_db):
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-dedupe")
    _create_route("acc-react-dedupe")
    ensure_account_state(account_id="acc-react-dedupe")
    upsert_reactivation_candidate(
        account_id="acc-react-dedupe",
        candidate={
            "id": "react-old",
            "type": "topic_followup",
            "text": "昨天相亲对象后来有找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    def fake_checker(*, candidate, **kwargs):
        return {
            "checked": True,
            "duplicate": candidate["id"] == "react-old",
            "reason": "same topic" if candidate["id"] == "react-old" else "different enough",
            "retry_count": 0,
        }

    def fake_regenerator(*, account_id, now, dedupe_feedback=None):
        return {
            "action": "reactivation_candidate_planned",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-new",
                "type": "topic_followup",
                "text": "今天回消息有没有轻松一点？",
                "scheduled_slot": "slot_1",
                "scheduled_at": "2026-06-05 12:15:00",
                "metadata": {"dedupe_feedback": dedupe_feedback},
            },
        }

    result = dispatch_reactivation_candidate(
        account_id="acc-react-dedupe",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
        dedupe_checker=fake_checker,
        regenerator=fake_regenerator,
    )

    assert result["action"] == "would_send"
    assert result["text"] == "今天回消息有没有轻松一点？"
    assert result["outbound_metadata"]["dedupe"]["duplicate"] is False
    assert result["outbound_metadata"]["dedupe"]["retry_count"] == 1


def test_dispatch_due_reactivation_sweep_sends_due_candidates_only(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.reactivation import (
        dispatch_due_reactivation_candidates,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-react-sweep")
    _create_route("acc-react-sweep")
    ensure_account_state(account_id="acc-react-sweep")
    upsert_reactivation_candidate(
        account_id="acc-react-sweep",
        candidate={
            "id": "react-sweep-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    # Before the slot: not due -> the sweep does not pick it up at all.
    early = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 11, 0),
        limit=10,
        dispatch_enabled=True,
        dry_run=True,
    )
    assert early == []

    # Kill-switch off: nothing dispatched even when due.
    off = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 12, 15),
        limit=10,
        dispatch_enabled=False,
        dry_run=False,
    )
    assert off == []

    # At the slot, dry-run: would_send, no real outbound created.
    results = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 12, 15),
        limit=10,
        dispatch_enabled=True,
        dry_run=True,
    )
    assert len(results) == 1
    assert results[0]["action"] == "would_send"
    assert results[0]["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert list_outbound_messages(account_id="acc-react-sweep") == []


def test_scan_due_does_not_overwrite_existing_candidate(fresh_db):
    from app.proactive.reactivation import (
        get_reactivation_candidate,
        upsert_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state, scan_due_proactive_account_checks

    _create_account("acc-react-notdue")
    _create_route("acc-react-notdue")
    ensure_account_state(
        account_id="acc-react-notdue",
        next_scan_at=datetime(2026, 6, 5, 12, 0),
    )
    upsert_reactivation_candidate(
        account_id="acc-react-notdue",
        candidate={
            "id": "react-notdue-1",
            "type": "topic_followup",
            "text": "昨天那个面试结果出来了吗？",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    # Planning runs for a due account, but an existing queued candidate must be
    # preserved (the slot + send happen in the dispatch sweep), so the planning
    # generators must NOT run and the candidate must not change.
    def _explode_topic_followup(**_):
        raise AssertionError("topic_followup generator must not run when a candidate exists")

    def _explode_content(**_):
        raise AssertionError("content_invitation generator must not run when a candidate exists")

    import app.proactive.state as proactive_state
    import app.proactive.account_checks as account_checks

    original_topic = account_checks.generate_topic_followup_candidate
    original_content = account_checks.generate_content_invitation_candidate
    account_checks.generate_topic_followup_candidate = _explode_topic_followup
    account_checks.generate_content_invitation_candidate = _explode_content
    proactive_state.generate_topic_followup_candidate = _explode_topic_followup
    proactive_state.generate_content_invitation_candidate = _explode_content
    try:
        results = scan_due_proactive_account_checks(
            now=datetime(2026, 6, 5, 14, 0),
            limit=10,
        )
    finally:
        account_checks.generate_topic_followup_candidate = original_topic
        account_checks.generate_content_invitation_candidate = original_content
        proactive_state.generate_topic_followup_candidate = original_topic
        proactive_state.generate_content_invitation_candidate = original_content

    assert results[0]["reactivation_planning"]["reason"] == "reactivation_candidate_pending"
    candidate = get_reactivation_candidate(account_id="acc-react-notdue")
    assert candidate["id"] == "react-notdue-1"
    assert candidate["scheduled_at"] == "2026-06-05 18:15:00"


def test_reactivation_slot_applies_send_jitter(fresh_db):
    from app.proactive.reactivation import next_reactivation_slot

    fresh_db.reactivation_send_jitter_min_seconds = 60
    fresh_db.reactivation_send_jitter_max_seconds = 120
    now = datetime(2026, 6, 5, 12, 0, 0)
    seen = set()
    for _ in range(25):
        slot = next_reactivation_slot(now=now)
        assert slot["scheduled_slot"] == "slot_1"
        t = datetime.fromisoformat(slot["scheduled_at"].replace(" ", "T"))
        # base slot 12:15:00 + forward jitter [60,120]s -> [12:16:00, 12:17:00]
        assert datetime(2026, 6, 5, 12, 16, 0) <= t <= datetime(2026, 6, 5, 12, 17, 0)
        seen.add(slot["scheduled_at"])
    assert len(seen) > 1  # offset is randomized, not constant


def test_upsert_proactive_account_state_metadata_patch_preserves_sibling_keys(fresh_db):
    from app.db import get_proactive_account_state, upsert_proactive_account_state

    _create_account("acc-patch")
    upsert_proactive_account_state(
        account_id="acc-patch",
        metadata={
            "commitments": [{"id": "c1"}],
            "reactivation_candidate": {"id": "r1", "type": "topic_followup", "text": "hi"},
            "other_key": "value",
        },
    )
    # Patch one key, set another, delete a third — siblings must survive.
    upsert_proactive_account_state(
        account_id="acc-patch",
        metadata_patch={
            "reactivation_candidate": {"id": "r2", "type": "topic_followup", "text": "hello"},
            "added_key": "added",
            "other_key": None,
        },
    )
    metadata = get_proactive_account_state(account_id="acc-patch")["metadata"]
    assert metadata["commitments"] == [{"id": "c1"}]
    assert metadata["reactivation_candidate"]["id"] == "r2"
    assert metadata["added_key"] == "added"
    assert "other_key" not in metadata


def test_plan_reactivation_persists_content_invitation_candidate(fresh_db):
    from app.proactive.reactivation import (
        REACTIVATION_TYPE_CONTENT_INVITATION,
        get_reactivation_candidate,
        plan_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-plan-content")
    ensure_account_state(account_id="acc-plan-content")

    def fake_content_generator(*, account_id, now):
        return {
            "action": "content_invitation_candidate_created",
            "account_id": account_id,
            "content_invitation": {
                "id": "cinv-plan-1",
                "topic": "中亚五国轻知识",
                "invitation_text": "Mark，要不要看几条中亚五国相关内容？",
                "title_items": [{"title": "标题一"}, {"title": "标题二"}, {"title": "标题三"}],
            },
            "evaluated_at": "2026-06-05 10:00:00",
        }

    result = plan_reactivation_candidate(
        account_id="acc-plan-content",
        now=datetime(2026, 6, 5, 10, 0),
        content_invitation_generator=fake_content_generator,
    )
    candidate = get_reactivation_candidate(account_id="acc-plan-content")

    assert result["action"] == "reactivation_candidate_planned"
    assert result["reactivation_type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    assert candidate["type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    assert candidate["content_invitation_id"] == "cinv-plan-1"
    assert candidate["text"] == "Mark，要不要看几条中亚五国相关内容？"
    assert candidate["metadata"]["title_count"] == 3


def test_plan_reactivation_topic_followup_takes_priority(fresh_db):
    from app.proactive.reactivation import (
        REACTIVATION_TYPE_TOPIC_FOLLOWUP,
        get_reactivation_candidate,
        plan_reactivation_candidate,
    )
    from app.proactive.state import ensure_account_state

    _create_account("acc-plan-topic")
    ensure_account_state(account_id="acc-plan-topic")
    calls = {"content": 0}

    def fake_topic_generator(*, account_id, now):
        return {
            "action": "topic_followup_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-topic-1",
                "type": "topic_followup",
                "topic": "亲子陪伴",
                "text": "昨晚小家伙睡得乖不乖？",
                "generated_at": "2026-06-05 10:00:00",
            },
        }

    def fake_content_generator(*, account_id, now):
        calls["content"] += 1
        return {"action": "no_op", "account_id": account_id, "reason": "should_not_call"}

    result = plan_reactivation_candidate(
        account_id="acc-plan-topic",
        now=datetime(2026, 6, 5, 10, 0),
        topic_followup_generator=fake_topic_generator,
        content_invitation_generator=fake_content_generator,
    )
    candidate = get_reactivation_candidate(account_id="acc-plan-topic")

    assert calls["content"] == 0
    assert result["reactivation_type"] == REACTIVATION_TYPE_TOPIC_FOLLOWUP
    assert result["content_invitation_generation"]["reason"] == "topic_followup_candidate_selected"
    assert candidate["type"] == REACTIVATION_TYPE_TOPIC_FOLLOWUP
    assert candidate["text"] == "昨晚小家伙睡得乖不乖？"
