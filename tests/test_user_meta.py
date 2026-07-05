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


def _rel_llm_response(
    stage: str = "acquainted", trust: str = "building", growth: str = "not_started"
) -> str:
    """关系状态天级 LLM 的有效输出；intensity>=2 的 run_once 测试需 patch 关系 LLM 引用。"""
    return json.dumps(
        {
            "relationship_stage": stage,
            "agent_need_trust_status": trust,
            "agent_need_growth_status": growth,
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

    with patch("app.user_meta_scheduler.generate_completion", return_value=_llm_response()) as mock_llm, \
         patch("app.relationship_state.generate_completion", return_value=_rel_llm_response()):
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

    with patch("app.user_meta_scheduler.generate_completion", return_value=_llm_response()) as mock_llm, \
         patch("app.relationship_state.generate_completion", return_value=_rel_llm_response()):
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

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm, \
         patch("app.relationship_state.generate_completion", return_value=_rel_llm_response()):
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

    with patch("app.user_meta_scheduler.generate_completion") as mock_llm, \
         patch("app.relationship_state.generate_completion", return_value=_rel_llm_response()):
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

    with patch("app.user_meta_scheduler.generate_completion", side_effect=RuntimeError("llm down")), \
         patch("app.relationship_state.generate_completion", return_value=_rel_llm_response()):
        result = asyncio.run(scheduler.run_once(now=datetime(2026, 6, 18, 3, 0, 0)))

    assert result["companion_failed"] == 1
    assert result["status"] == "partial_error"
    # 关系 LLM 被独立 patch 为成功，errors 仅含 companion 失败一条
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
    body = res.json()
    assert body["meta"] is None
    # 无 meta 时 relationship_view 按默认值渲染（Phase D）
    assert "当前阶段：破冰" in body["relationship_view"]


def test_admin_get_meta_relationship_view_reflects_db(client, fresh_db):
    from app.db import update_account_user_meta_relationship

    _create_account("acc-admin-view")
    update_account_user_meta_relationship(
        account_id="acc-admin-view",
        relationship_stage="deep_bond",
        agent_need_survival_status="healthy",
        agent_need_trust_status="stable",
        agent_need_growth_status="emerging",
    )

    res = client.get("/admin/accounts/acc-admin-view/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    view = res.json()["relationship_view"]
    assert "当前阶段：挚友/热恋" in view
    assert "生存 / 活跃：健康" in view
    assert "信任与尊重：稳定" in view
    assert "共同成长：有苗头" in view


def test_admin_get_meta_mission_null_when_unassigned(client, fresh_db):
    """未分配使命：mission 字段为 None（与 meta 为 None 时的表达一致）。"""
    _create_account("acc-admin-no-mission")

    res = client.get("/admin/accounts/acc-admin-no-mission/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    assert res.json()["mission"] is None


def test_admin_get_meta_mission_reflects_assignment_and_progress(client, fresh_db):
    """已分配使命：mission 字段包含 agent_mission_and_orchestration_design.md §7 定义的信息。"""
    from app.db import assign_mission, record_mission_moment

    _create_account("acc-admin-mission")
    assign_mission(account_id="acc-admin-mission", mission_id="mission_002")
    record_mission_moment(account_id="acc-admin-mission", mission_id="mission_002", content="地铁上的一次相视一笑")

    res = client.get("/admin/accounts/acc-admin-mission/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    mission = res.json()["mission"]
    assert mission["mission_id"] == "mission_002"
    assert mission["display_name"] == "十刻"
    assert mission["progress"] == 1
    assert mission["target_count"] == 10
    assert mission["progress_label"] == "1/10"
    assert mission["recent_moments"] == ["地铁上的一次相视一笑"]


def test_admin_get_meta_mission_none_for_unresolved_mission_id(client, fresh_db):
    """account_mission 引用了未注册的 mission_id（脏数据/模板下线）：mission 字段降级为 None，不 500。"""
    from app.db import assign_mission

    _create_account("acc-admin-unknown-mission")
    assign_mission(account_id="acc-admin-unknown-mission", mission_id="mission_999")

    res = client.get("/admin/accounts/acc-admin-unknown-mission/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    assert res.json()["mission"] is None


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


# ---------------------------------------------------------------------------
# 关系状态结构化字段（Phase A：见 relationship_state_implementation_plan_tmp.md）
# ---------------------------------------------------------------------------

REL_FIELDS = (
    "relationship_stage",
    "agent_need_survival_status",
    "agent_need_trust_status",
    "agent_need_growth_status",
)
REL_DEFAULTS = {
    "relationship_stage": "icebreaking",
    "agent_need_survival_status": "cooling",
    "agent_need_trust_status": "building",
    "agent_need_growth_status": "not_started",
}


def _companion_kwargs(account_id: str) -> dict:
    return dict(
        account_id=account_id,
        registered_at="2026-06-01 10:00:00",
        message_intensity_level=2,
        companion_primary_type="daily_chat",
        companion_secondary_types=["practical_assistant"],
        companion_type_confidence=0.6,
        companion_type_last_evaluated_at="2026-06-18 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="测试",
        safety_risk_trigger_count_30d=1,
        last_evaluated_at="2026-06-18 03:00:00",
    )


def test_relationship_defaults_on_fresh_meta(fresh_db):
    """upsert 不写关系列；migration 默认值生效。"""
    from app.db import get_account_user_meta, upsert_account_user_meta

    _create_account("acc-rel-default")
    upsert_account_user_meta(**_companion_kwargs("acc-rel-default"))

    meta = get_account_user_meta(account_id="acc-rel-default")
    assert {k: meta[k] for k in REL_FIELDS} == REL_DEFAULTS


def test_daily_snapshot_relationship_defaults(fresh_db):
    """account_user_meta_daily 补列且 None 入参回退默认。"""
    from app.db import connect, insert_account_user_meta_daily

    _create_account("acc-rel-daily-default")
    insert_account_user_meta_daily(
        snapshot_date="2026-06-18", **_companion_kwargs("acc-rel-daily-default")
    )

    with connect() as conn:
        row = dict(
            conn.execute(
                "SELECT relationship_stage, agent_need_survival_status, "
                "agent_need_trust_status, agent_need_growth_status "
                "FROM account_user_meta_daily "
                "WHERE account_id = ? AND snapshot_date = ?",
                ("acc-rel-daily-default", "2026-06-18"),
            ).fetchone()
        )
    assert row == REL_DEFAULTS


def test_update_relationship_builds_row_with_defaults(fresh_db):
    """无 meta 行时 setter 建行，写入指定列、其余取默认。"""
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-rel-build")
    update_account_user_meta_relationship(
        account_id="acc-rel-build", relationship_stage="acquainted"
    )

    meta = get_account_user_meta(account_id="acc-rel-build")
    assert meta is not None
    assert meta["relationship_stage"] == "acquainted"
    assert meta["agent_need_survival_status"] == "cooling"
    assert meta["agent_need_trust_status"] == "building"
    assert meta["agent_need_growth_status"] == "not_started"
    assert meta["registered_at"]  # registered_at 取 accounts.created_at，非空


def test_update_relationship_partial_keeps_others(fresh_db):
    """只更新传入列；其余关系列保持不变。"""
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-rel-partial")
    update_account_user_meta_relationship(
        account_id="acc-rel-partial",
        relationship_stage="deep_bond",
        agent_need_survival_status="healthy",
        agent_need_trust_status="stable",
        agent_need_growth_status="emerging",
    )
    update_account_user_meta_relationship(
        account_id="acc-rel-partial", agent_need_survival_status="inactive"
    )

    meta = get_account_user_meta(account_id="acc-rel-partial")
    assert meta["agent_need_survival_status"] == "inactive"
    assert meta["relationship_stage"] == "deep_bond"
    assert meta["agent_need_trust_status"] == "stable"
    assert meta["agent_need_growth_status"] == "emerging"


def test_update_relationship_all_none_is_noop(fresh_db):
    """全 None 入参不建行、不报错。"""
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-rel-noop")
    update_account_user_meta_relationship(account_id="acc-rel-noop")

    assert get_account_user_meta(account_id="acc-rel-noop") is None


def test_update_relationship_invalid_enum_falls_back(fresh_db):
    """非法 enum 值回退该列默认。"""
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-rel-bad")
    update_account_user_meta_relationship(
        account_id="acc-rel-bad",
        relationship_stage="bogus",
        agent_need_survival_status="???",
    )

    meta = get_account_user_meta(account_id="acc-rel-bad")
    assert meta["relationship_stage"] == "icebreaking"
    assert meta["agent_need_survival_status"] == "cooling"


def test_update_relationship_account_not_found(fresh_db):
    """账号不存在时抛 ValueError。"""
    import pytest

    from app.db import update_account_user_meta_relationship

    with pytest.raises(ValueError):
        update_account_user_meta_relationship(
            account_id="acc-ghost", relationship_stage="acquainted"
        )


def test_update_relationship_preserves_companion(fresh_db):
    """setter 不改 companion 字段与 last_evaluated_at。"""
    from app.db import (
        get_account_user_meta,
        update_account_user_meta_relationship,
        upsert_account_user_meta,
    )

    _create_account("acc-rel-keep-companion")
    upsert_account_user_meta(**_companion_kwargs("acc-rel-keep-companion"))
    update_account_user_meta_relationship(
        account_id="acc-rel-keep-companion", relationship_stage="deep_bond"
    )

    meta = get_account_user_meta(account_id="acc-rel-keep-companion")
    assert meta["relationship_stage"] == "deep_bond"
    assert meta["companion_primary_type"] == "daily_chat"
    assert meta["companion_secondary_types"] == ["practical_assistant"]
    assert meta["last_evaluated_at"] == "2026-06-18 03:00:00"


def test_upsert_does_not_reset_relationship(fresh_db):
    """每日 companion 刷新（upsert）不重置关系列。"""
    from app.db import (
        get_account_user_meta,
        update_account_user_meta_relationship,
        upsert_account_user_meta,
    )

    _create_account("acc-rel-upsert-keep")
    update_account_user_meta_relationship(
        account_id="acc-rel-upsert-keep",
        relationship_stage="deep_bond",
        agent_need_survival_status="healthy",
    )
    upsert_account_user_meta(
        **{**_companion_kwargs("acc-rel-upsert-keep"), "message_intensity_level": 5}
    )

    meta = get_account_user_meta(account_id="acc-rel-upsert-keep")
    assert meta["message_intensity_level"] == 5
    assert meta["relationship_stage"] == "deep_bond"
    assert meta["agent_need_survival_status"] == "healthy"


def test_set_companion_manual_keeps_relationship(fresh_db):
    """set_companion_type_manual 不覆盖关系列。"""
    from app.db import (
        get_account_user_meta,
        set_companion_type_manual,
        update_account_user_meta_relationship,
    )

    _create_account("acc-rel-manual-keep")
    update_account_user_meta_relationship(
        account_id="acc-rel-manual-keep", relationship_stage="acquainted"
    )
    set_companion_type_manual(
        account_id="acc-rel-manual-keep",
        primary_type="practical_assistant",
        secondary_types=[],
        confidence=1.0,
        expires_at=None,
        reasoning="人工",
        now="2026-06-19 03:00:00",
    )

    meta = get_account_user_meta(account_id="acc-rel-manual-keep")
    assert meta["companion_type_source"] == "manual"
    assert meta["relationship_stage"] == "acquainted"


def test_daily_snapshot_captures_current_relationship(fresh_db):
    """insert_daily 把传入的当前关系值快照进每日历史。"""
    from app.db import (
        connect,
        get_account_user_meta,
        insert_account_user_meta_daily,
        update_account_user_meta_relationship,
    )

    _create_account("acc-rel-daily-capture")
    update_account_user_meta_relationship(
        account_id="acc-rel-daily-capture",
        relationship_stage="deep_bond",
        agent_need_survival_status="healthy",
        agent_need_trust_status="stable",
        agent_need_growth_status="emerging",
    )
    cur = get_account_user_meta(account_id="acc-rel-daily-capture")
    insert_account_user_meta_daily(
        snapshot_date="2026-06-18",
        relationship_stage=cur["relationship_stage"],
        agent_need_survival_status=cur["agent_need_survival_status"],
        agent_need_trust_status=cur["agent_need_trust_status"],
        agent_need_growth_status=cur["agent_need_growth_status"],
        **_companion_kwargs("acc-rel-daily-capture"),
    )

    with connect() as conn:
        row = dict(
            conn.execute(
                "SELECT relationship_stage, agent_need_survival_status, "
                "agent_need_trust_status, agent_need_growth_status "
                "FROM account_user_meta_daily "
                "WHERE account_id = ? AND snapshot_date = ?",
                ("acc-rel-daily-capture", "2026-06-18"),
            ).fetchone()
        )
    assert row == {
        "relationship_stage": "deep_bond",
        "agent_need_survival_status": "healthy",
        "agent_need_trust_status": "stable",
        "agent_need_growth_status": "emerging",
    }


def test_admin_get_meta_includes_relationship_fields(client, fresh_db):
    """GET /admin/accounts/{id}/meta 返回四个关系字段。"""
    from app.db import update_account_user_meta_relationship

    _create_account("acc-rel-admin")
    update_account_user_meta_relationship(
        account_id="acc-rel-admin",
        relationship_stage="acquainted",
        agent_need_growth_status="emerging",
    )

    res = client.get("/admin/accounts/acc-rel-admin/meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    meta = res.json()["meta"]
    assert meta["relationship_stage"] == "acquainted"
    assert meta["agent_need_survival_status"] == "cooling"
    assert meta["agent_need_trust_status"] == "building"
    assert meta["agent_need_growth_status"] == "emerging"


def test_admin_list_user_meta_includes_relationship_fields(client, fresh_db):
    """GET /admin/user-meta 列表带出四个关系字段。"""
    from app.db import update_account_user_meta_relationship

    _create_account("acc-rel-admin-list")
    update_account_user_meta_relationship(
        account_id="acc-rel-admin-list", relationship_stage="deep_bond"
    )

    res = client.get("/admin/user-meta", headers=ADMIN_HEADERS)

    assert res.status_code == 200
    by_id = {item["account_id"]: item for item in res.json()["items"]}
    row = by_id["acc-rel-admin-list"]
    assert row["relationship_stage"] == "deep_bond"
    assert all(field in row for field in REL_FIELDS)
