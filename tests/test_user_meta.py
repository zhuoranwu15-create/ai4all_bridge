import asyncio
import json
from datetime import datetime
from unittest.mock import patch


ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}
STAFF_HEADERS = {"Authorization": "Bearer test-staff"}


def _create_account(account_id: str, *, is_debug: bool = False) -> int:
    from app.db import get_or_create_session, set_account_debug_flag

    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    if is_debug:
        set_account_debug_flag(account_id=account_id, is_debug=True)
    return int(state["session"]["id"])


def _insert_message_at(
    *,
    account_id: str,
    session_id: int,
    created_at: str,
    direction: str = "inbound",
    role: str = "user",
    message_type: str = "text",
    content: str = "hello",
) -> int:
    from app.db import connect, insert_message

    message_id = f"msg-{account_id}-{created_at}-{direction}-{content}"
    row_id = insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=message_id,
        reply_to_message_id=None,
        direction=direction,
        role=role,
        message_type=message_type,
        content=content,
        raw={},
    )
    assert row_id is not None
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )
    return int(row_id)


def _seed_inbound_messages(
    *, account_id: str, session_id: int, count: int, date: str = "2026-06-17"
) -> None:
    for index in range(count):
        _insert_message_at(
            account_id=account_id,
            session_id=session_id,
            created_at=f"{date} 10:{index:02d}:00",
            content=f"message {index}",
        )


def _insert_moderation_task(
    *,
    account_id: str,
    source_id: str,
    risk_level: str,
    created_at: str,
    direction: str = "inbound",
    source_type: str = "message",
) -> None:
    from app.db import connect, create_content_moderation_task

    task = create_content_moderation_task(
        account_id=account_id,
        session_id=None,
        source_type=source_type,
        source_id=source_id,
        message_db_id=None,
        outbound_message_id=None,
        direction=direction,
        content_kind="text",
        status="needs_review" if risk_level not in {"pass", "safe"} else "machine_passed",
        risk_level=risk_level,
        risk_categories=[],
        policy_version="test_policy",
        idempotency_key=f"{account_id}:{source_type}:{source_id}:{risk_level}:{direction}:{created_at}",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE content_moderation_tasks SET created_at = ? WHERE id = ?",
            (created_at, task["id"]),
        )


def _daily_count(account_id: str, snapshot_date: str) -> int:
    from app.db import connect

    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM account_user_meta_daily
            WHERE account_id = ? AND snapshot_date = ?
            """,
            (account_id, snapshot_date),
        ).fetchone()
    return int(row["c"] or 0)


def _llm_response(primary: str = "practical_assistant") -> str:
    return json.dumps(
        {
            "primary_type": primary,
            "secondary_types": ["daily_chat"],
            "confidence": 0.82,
            "reasoning": "用户近期主要在提出任务和整理类需求",
        },
        ensure_ascii=False,
    )


def test_message_intensity_zero(fresh_db):
    from app.db import compute_message_intensity

    _create_account("acc-int-zero")

    assert (
        compute_message_intensity(
            account_id="acc-int-zero",
            today_start="2026-06-18 00:00:00",
        )
        == 0
    )


def test_message_intensity_today_excluded(fresh_db):
    from app.db import compute_message_intensity

    session_id = _create_account("acc-int-today")
    _insert_message_at(
        account_id="acc-int-today",
        session_id=session_id,
        created_at="2026-06-18 00:01:00",
    )

    assert (
        compute_message_intensity(
            account_id="acc-int-today",
            today_start="2026-06-18 00:00:00",
        )
        == 0
    )


def test_message_intensity_formula(fresh_db):
    from app.db import compute_message_intensity

    session_id = _create_account("acc-int-formula")
    _seed_inbound_messages(account_id="acc-int-formula", session_id=session_id, count=7)

    assert (
        compute_message_intensity(
            account_id="acc-int-formula",
            today_start="2026-06-18 00:00:00",
        )
        == 2
    )


def test_safety_risk_pass_and_safe_excluded(fresh_db):
    from app.db import compute_safety_risk_count_30d

    _create_account("acc-risk-safe")
    _insert_moderation_task(
        account_id="acc-risk-safe",
        source_id="m-pass",
        risk_level="pass",
        created_at="2026-06-17 10:00:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-safe",
        source_id="m-safe",
        risk_level="safe",
        created_at="2026-06-17 10:01:00",
    )

    assert (
        compute_safety_risk_count_30d(
            account_id="acc-risk-safe",
            thirty_days_ago="2026-05-19 10:00:00",
        )
        == 0
    )


def test_safety_risk_includes_unknown_review_block_and_dedups(fresh_db):
    from app.db import compute_safety_risk_count_30d

    _create_account("acc-risk-hit")
    _insert_moderation_task(
        account_id="acc-risk-hit",
        source_id="m-unknown",
        risk_level="unknown",
        created_at="2026-06-17 10:00:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-hit",
        source_id="m-review",
        risk_level="review",
        created_at="2026-06-17 10:01:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-hit",
        source_id="m-review",
        risk_level="block",
        created_at="2026-06-17 10:02:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-hit",
        source_id="m-escalate",
        risk_level="escalate",
        created_at="2026-06-17 10:03:00",
    )

    assert (
        compute_safety_risk_count_30d(
            account_id="acc-risk-hit",
            thirty_days_ago="2026-05-19 10:00:00",
        )
        == 3
    )


def test_safety_risk_excludes_outbound_non_message_and_window(fresh_db):
    from app.db import compute_safety_risk_count_30d

    _create_account("acc-risk-filter")
    _insert_moderation_task(
        account_id="acc-risk-filter",
        source_id="m-outbound",
        risk_level="block",
        direction="outbound",
        created_at="2026-06-17 10:00:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-filter",
        source_id="outbound-1",
        source_type="outbound_message",
        risk_level="block",
        created_at="2026-06-17 10:01:00",
    )
    _insert_moderation_task(
        account_id="acc-risk-filter",
        source_id="m-old",
        risk_level="block",
        created_at="2026-05-18 09:59:00",
    )

    assert (
        compute_safety_risk_count_30d(
            account_id="acc-risk-filter",
            thirty_days_ago="2026-05-19 10:00:00",
        )
        == 0
    )


def test_upsert_and_daily_idempotent(fresh_db):
    from app.db import get_account_user_meta, insert_account_user_meta_daily, upsert_account_user_meta

    _create_account("acc-meta-upsert")
    kwargs = dict(
        account_id="acc-meta-upsert",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=1,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.5,
        companion_type_last_evaluated_at="2026-06-18 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="初始",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-18 03:00:00",
    )
    upsert_account_user_meta(**kwargs)
    upsert_account_user_meta(**{**kwargs, "message_intensity_level": 3})
    insert_account_user_meta_daily(snapshot_date="2026-06-18", **kwargs)
    insert_account_user_meta_daily(snapshot_date="2026-06-18", **{**kwargs, "message_intensity_level": 9})

    assert get_account_user_meta(account_id="acc-meta-upsert")["message_intensity_level"] == 3
    assert _daily_count("acc-meta-upsert", "2026-06-18") == 1


def test_set_companion_manual_sets_source(fresh_db):
    from app.db import get_account_user_meta, set_companion_type_manual

    _create_account("acc-manual")
    set_companion_type_manual(
        account_id="acc-manual",
        primary_type="practical_assistant",
        secondary_types=["daily_chat"],
        confidence=1.0,
        expires_at="2026-07-18 00:00:00",
        reasoning="用户明确反馈",
        now="2026-06-18 03:00:00",
    )

    meta = get_account_user_meta(account_id="acc-manual")
    assert meta["companion_type_source"] == "manual"
    assert meta["companion_primary_type"] == "practical_assistant"
    assert meta["companion_type_expires_at"] == "2026-07-18 00:00:00"


def test_fetch_recent_inbound_excludes_moderation_blocked(fresh_db):
    from app.db import fetch_recent_inbound_messages, mark_message_moderation_blocked

    session_id = _create_account("acc-fetch")
    blocked_id = _insert_message_at(
        account_id="acc-fetch",
        session_id=session_id,
        created_at="2026-06-17 10:00:00",
        content="blocked text",
    )
    _insert_message_at(
        account_id="acc-fetch",
        session_id=session_id,
        created_at="2026-06-17 10:01:00",
        content="visible text",
    )
    _insert_message_at(
        account_id="acc-fetch",
        session_id=session_id,
        created_at="2026-06-17 10:02:00",
        message_type="image",
        content="image text",
    )
    mark_message_moderation_blocked(message_db_id=blocked_id, account_id="acc-fetch")

    messages = fetch_recent_inbound_messages(account_id="acc-fetch", limit=50)

    assert [message["content"] for message in messages] == ["visible text"]


def test_run_once_skips_debug_accounts(fresh_db):
    from app.db import get_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    _create_account("acc-debug", is_debug=True)
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["processed"] == 0
    assert get_account_user_meta(account_id="acc-debug") is None


def test_run_once_skips_low_signal_companion(fresh_db):
    from app.db import get_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-low-signal")
    _insert_message_at(
        account_id="acc-low-signal",
        session_id=session_id,
        created_at="2026-06-17 10:00:00",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm:
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["processed"] == 1
    assert result["companion_evaluated"] == 0
    mock_llm.assert_not_called()
    meta = get_account_user_meta(account_id="acc-low-signal")
    assert meta["message_intensity_level"] == 0
    assert meta["companion_primary_type"] is None
    assert meta["companion_type_last_evaluated_at"] is not None


def test_run_once_low_signal_account_classifies_when_signal_reaches_threshold(fresh_db):
    from app.db import get_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-low-then-ready")
    _insert_message_at(
        account_id="acc-low-then-ready",
        session_id=session_id,
        created_at="2026-06-17 10:00:00",
        content="first",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm:
        asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))
    mock_llm.assert_not_called()
    assert get_account_user_meta(account_id="acc-low-then-ready")["companion_primary_type"] is None

    for index in range(6):
        _insert_message_at(
            account_id="acc-low-then-ready",
            session_id=session_id,
            created_at=f"2026-06-18 10:{index:02d}:00",
            content=f"ready {index}",
        )

    with patch("app.user_meta_scheduler.generate_completion", return_value=_llm_response()) as mock_llm:
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 19, 3, 0, 0)))

    assert result["companion_evaluated"] == 1
    assert mock_llm.call_count == 1
    meta = get_account_user_meta(account_id="acc-low-then-ready")
    assert meta["message_intensity_level"] == 2
    assert meta["companion_primary_type"] == "practical_assistant"


def test_run_once_companion_reval_after_7d(fresh_db):
    from app.db import get_account_user_meta, upsert_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-reval")
    _seed_inbound_messages(account_id="acc-reval", session_id=session_id, count=7)
    upsert_account_user_meta(
        account_id="acc-reval",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.4,
        companion_type_last_evaluated_at="2026-06-10 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="旧值",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-10 03:00:00",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion", return_value=_llm_response()) as mock_llm:
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["companion_evaluated"] == 1
    assert mock_llm.call_count == 1
    meta = get_account_user_meta(account_id="acc-reval")
    assert meta["companion_primary_type"] == "practical_assistant"
    assert meta["companion_type_source"] == "auto"


def test_run_once_companion_no_reval_within_7d(fresh_db):
    from app.db import get_account_user_meta, upsert_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-no-reval")
    _seed_inbound_messages(account_id="acc-no-reval", session_id=session_id, count=7)
    upsert_account_user_meta(
        account_id="acc-no-reval",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.4,
        companion_type_last_evaluated_at="2026-06-16 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="保留",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-16 03:00:00",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm:
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["companion_evaluated"] == 0
    mock_llm.assert_not_called()
    assert get_account_user_meta(account_id="acc-no-reval")["companion_primary_type"] == "daily_chat"


def test_run_once_manual_override_not_overwritten(fresh_db):
    from app.db import get_account_user_meta, upsert_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-manual-keep")
    _seed_inbound_messages(account_id="acc-manual-keep", session_id=session_id, count=7)
    upsert_account_user_meta(
        account_id="acc-manual-keep",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="work_career",
        companion_secondary_types=[],
        companion_type_confidence=1.0,
        companion_type_last_evaluated_at="2026-06-01 03:00:00",
        companion_type_source="manual",
        companion_type_expires_at="2026-07-01 00:00:00",
        companion_type_reasoning="人工覆盖",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-01 03:00:00",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm:
        asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    mock_llm.assert_not_called()
    assert get_account_user_meta(account_id="acc-manual-keep")["companion_primary_type"] == "work_career"


def test_run_once_idempotent_same_day(fresh_db):
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-idem")
    _seed_inbound_messages(account_id="acc-idem", session_id=session_id, count=1)
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))
    asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 4, 0, 0)))

    assert _daily_count("acc-idem", "2026-06-18") == 1


def test_run_once_lm_failure_keeps_existing(fresh_db):
    from app.db import get_account_user_meta, upsert_account_user_meta
    from app.user_meta_scheduler import UserMetaScheduler

    session_id = _create_account("acc-llm-fail")
    _seed_inbound_messages(account_id="acc-llm-fail", session_id=session_id, count=7)
    upsert_account_user_meta(
        account_id="acc-llm-fail",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.4,
        companion_type_last_evaluated_at="2026-06-10 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="旧值",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-10 03:00:00",
    )
    scheduler = UserMetaScheduler(page_size=10, inter_account_sleep=0.0)

    with patch("app.user_meta_scheduler.generate_completion", side_effect=RuntimeError("llm down")):
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["companion_failed"] == 1
    assert result["status"] == "partial_error"
    assert result["errors"] == [
        {
            "account_id": "acc-llm-fail",
            "step": "companion_classification",
            "error": "llm down",
        }
    ]
    assert get_account_user_meta(account_id="acc-llm-fail")["companion_primary_type"] == "daily_chat"


def test_wipe_account_data_clears_user_meta(fresh_db):
    from app.db import get_account_user_meta, upsert_account_user_meta, wipe_account_data

    _create_account("acc-wipe-meta")
    upsert_account_user_meta(
        account_id="acc-wipe-meta",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.4,
        companion_type_last_evaluated_at="2026-06-10 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="旧值",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-10 03:00:00",
    )

    stats = wipe_account_data(account_id="acc-wipe-meta")

    assert stats["account_user_meta_deleted"] == 1
    assert get_account_user_meta(account_id="acc-wipe-meta") is None


def test_admin_get_meta_returns_all_fields(client, fresh_db):
    from app.db import upsert_account_user_meta

    _create_account("acc-admin-meta")
    upsert_account_user_meta(
        account_id="acc-admin-meta",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=4,
        companion_primary_type="emotional_support",
        companion_secondary_types=["daily_chat"],
        companion_type_confidence=0.82,
        companion_type_last_evaluated_at="2026-06-18 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="用户频繁表达压力和孤独感",
        safety_risk_trigger_count_30d=2,
        last_evaluated_at="2026-06-18 03:00:00",
    )

    res = client.get("/admin/accounts/acc-admin-meta/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    body = res.json()
    meta = body["meta"]
    assert meta["account_id"] == "acc-admin-meta"
    assert meta["message_intensity_level"] == 4
    assert meta["companion_secondary_types"] == ["daily_chat"]
    assert "content" not in meta


def test_admin_get_meta_null_when_not_evaluated(client, fresh_db):
    _create_account("acc-admin-null")

    res = client.get("/admin/accounts/acc-admin-null/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    assert res.json() == {"meta": None}


def test_admin_list_user_meta_returns_account_rows(client, fresh_db):
    from app.db import upsert_account_user_meta

    _create_account("acc-meta-list-ready")
    _create_account("acc-meta-list-missing")
    upsert_account_user_meta(
        account_id="acc-meta-list-ready",
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=3,
        companion_primary_type="work_career",
        companion_secondary_types=["practical_assistant"],
        companion_type_confidence=0.91,
        companion_type_last_evaluated_at="2026-06-18 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="用户主要讨论工作事项",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-18 03:00:00",
    )

    res = client.get("/admin/user-meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    items = res.json()["items"]
    by_id = {item["account_id"]: item for item in items}
    assert by_id["acc-meta-list-ready"]["companion_primary_type"] == "work_career"
    assert by_id["acc-meta-list-ready"]["companion_secondary_types"] == ["practical_assistant"]
    assert by_id["acc-meta-list-missing"]["last_evaluated_at"] is None


def test_admin_list_user_meta_filters_primary_type(client, fresh_db):
    from app.db import upsert_account_user_meta

    _create_account("acc-meta-filter-work")
    _create_account("acc-meta-filter-chat")
    for account_id, primary_type in (
        ("acc-meta-filter-work", "work_career"),
        ("acc-meta-filter-chat", "daily_chat"),
    ):
        upsert_account_user_meta(
            account_id=account_id,
            registered_at="2026-06-01 10:00:00",
            message_intensity_level=3,
            companion_primary_type=primary_type,
            companion_secondary_types=[],
            companion_type_confidence=0.8,
            companion_type_last_evaluated_at="2026-06-18 03:00:00",
            companion_type_source="auto",
            companion_type_expires_at=None,
            companion_type_reasoning="测试",
            safety_risk_trigger_count_30d=0,
            last_evaluated_at="2026-06-18 03:00:00",
        )

    res = client.get(
        "/admin/user-meta?primary_type=work_career",
        headers=ADMIN_HEADERS,
    )

    assert res.status_code == 200
    ids = {item["account_id"] for item in res.json()["items"]}
    assert "acc-meta-filter-work" in ids
    assert "acc-meta-filter-chat" not in ids


def test_admin_patch_companion_sets_manual_source(client, fresh_db):
    _create_account("acc-admin-patch")

    res = client.patch(
        "/admin/accounts/acc-admin-patch/meta/companion",
        headers=ADMIN_HEADERS,
        json={
            "primary_type": "practical_assistant",
            "secondary_types": [],
            "confidence": 1.0,
            "expires_at": "2026-07-18 00:00:00",
            "reason": "用户明确反馈",
        },
    )

    assert res.status_code == 200
    meta = res.json()["meta"]
    assert meta["companion_type_source"] == "manual"
    assert meta["companion_primary_type"] == "practical_assistant"


def test_admin_patch_companion_requires_admin(client, fresh_db):
    _create_account("acc-admin-staff")

    res = client.patch(
        "/admin/accounts/acc-admin-staff/meta/companion",
        headers=STAFF_HEADERS,
        json={
            "primary_type": "practical_assistant",
            "secondary_types": [],
            "confidence": 1.0,
        },
    )

    assert res.status_code == 403


def test_admin_user_meta_run_once_requires_admin(client, fresh_db):
    res = client.post("/admin/ops/user-meta/run-once", headers=STAFF_HEADERS)

    assert res.status_code == 403
