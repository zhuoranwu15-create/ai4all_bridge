from typing import Optional


def _ensure_account(account_id: str = "acc-worker") -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _queued_task(
    *,
    account_id: str = "acc-worker",
    text: str,
    metadata: Optional[dict] = None,
):
    from app.db import create_content_moderation_task

    _ensure_account(account_id)
    return create_content_moderation_task(
        app_id="zhaoxi",
        account_id=account_id,
        session_id=None,
        source_type="message",
        source_id=f"source-{account_id}-{abs(hash(text))}",
        message_db_id=None,
        outbound_message_id=None,
        direction="inbound",
        content_kind="text",
        status="queued",
        risk_level="unknown",
        risk_categories=[],
        snapshot_text=text,
        media={},
        sampling_reason="test",
        sample_rate_percent=100,
        policy_version="test_policy",
        idempotency_key=f"worker:{account_id}:{abs(hash(text))}",
        metadata=metadata or {},
    )


def test_worker_passes_safe_queued_task(fresh_db):
    from app.db import get_content_moderation_task, list_content_moderation_results
    from app.platform.moderation.worker import run_once

    task = _queued_task(text="normal text for worker")
    summary = run_once(batch_size=1)
    updated = get_content_moderation_task(task_id=task["id"])
    results = list_content_moderation_results(task_id=task["id"])

    assert summary["counts"]["machine_passed"] == 1
    assert updated["status"] == "machine_passed"
    assert updated["risk_level"] == "pass"
    assert updated["machine_attempts"] == 1
    assert updated["machine_claimed_at"] is None
    assert any(result["reviewer_type"] == "rule" for result in results)


def test_worker_rule_hit_enters_review_and_updates_risk_state(fresh_db):
    from app.db import get_content_moderation_task, get_moderation_account_risk_state
    from app.platform.moderation.worker import run_once

    task = _queued_task(text="needs MODERATION_TEST_REVIEW")
    run_once(batch_size=1)
    updated = get_content_moderation_task(task_id=task["id"])
    risk_state = get_moderation_account_risk_state(account_id="acc-worker")

    assert updated["status"] == "needs_review"
    assert updated["risk_level"] == "review"
    assert updated["risk_categories"] == ["test_review"]
    assert risk_state["risk_level"] == "elevated"
    assert risk_state["risk_score"] == 1
    assert risk_state["sample_multiplier"] == 2.0


def test_worker_aggregates_llm_result(monkeypatch, fresh_db):
    from app.db import get_content_moderation_task, get_moderation_account_risk_state
    from app.platform.moderation.models import MachineReviewResult
    from app.platform.moderation import worker

    task = _queued_task(
        text="normal text selected for llm",
        metadata={"llm_review_selected": True},
    )

    def _review(**kwargs):
        return MachineReviewResult(
            reviewer_type="llm",
            engine="fake",
            engine_version="test",
            level="block",
            categories=["llm_block"],
            confidence=0.7,
            reason="test block",
        )

    monkeypatch.setattr(worker.llm_review, "review_text_with_llm", _review)
    worker.run_once(batch_size=1)
    updated = get_content_moderation_task(task_id=task["id"])
    risk_state = get_moderation_account_risk_state(account_id="acc-worker")

    assert updated["status"] == "needs_review"
    assert updated["risk_level"] == "block"
    assert updated["risk_categories"] == ["llm_block"]
    assert updated["confidence"] == 0.7
    assert risk_state["risk_score"] == 3
    assert risk_state["sample_multiplier"] == 3.0


def test_worker_retries_then_moves_failed_task_to_review(monkeypatch, fresh_db):
    from app.db import get_content_moderation_task, list_content_moderation_results
    from app.platform.moderation import worker

    fresh_db.moderation_worker_max_attempts = 2
    task = _queued_task(
        text="normal text but llm fails",
        metadata={"llm_review_selected": True},
    )

    def _fail(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(worker.llm_review, "review_text_with_llm", _fail)

    first = worker.run_once(batch_size=1)
    after_first = get_content_moderation_task(task_id=task["id"])
    second = worker.run_once(batch_size=1)
    after_second = get_content_moderation_task(task_id=task["id"])
    results = list_content_moderation_results(task_id=task["id"])

    assert first["counts"]["retry"] == 1
    assert after_first["status"] == "queued"
    assert after_first["machine_attempts"] == 1
    assert after_first["machine_claimed_at"] is None
    assert second["counts"]["needs_review"] == 1
    assert after_second["status"] == "needs_review"
    assert after_second["risk_level"] == "review"
    assert after_second["last_error"].startswith("machine_review_failed:")
    assert after_second["machine_attempts"] == 2
    assert any(result["reviewer_type"] == "system" and result["result_level"] == "error" for result in results)


def test_worker_claims_stale_task_only_once(fresh_db):
    from app.db import claim_queued_content_moderation_tasks

    task = _queued_task(text="claim me once")
    first = claim_queued_content_moderation_tasks(batch_size=1, claim_timeout_seconds=300)
    second = claim_queued_content_moderation_tasks(batch_size=1, claim_timeout_seconds=300)

    assert [item["id"] for item in first] == [task["id"]]
    assert second == []
