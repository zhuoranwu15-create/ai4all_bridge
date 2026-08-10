from typing import Any, Dict, Optional


def _ensure_account(account_id: str = "acc-worker", app_id: str = "zhaoxi") -> None:
    from app.db import connect

    with connect() as conn:
        conn.execute("INSERT INTO accounts(id, app_id) VALUES (?, ?)", (account_id, app_id))


def _queued_task(
    *,
    account_id: str = "acc-worker",
    app_id: str = "zhaoxi",
    text: str,
    content_kind: str = "text",
    media: Optional[Dict[str, Any]] = None,
    metadata: Optional[dict] = None,
):
    from app.db import create_content_moderation_task

    _ensure_account(account_id, app_id)
    return create_content_moderation_task(
        app_id=app_id,
        account_id=account_id,
        session_id=None,
        source_type="message",
        source_id=f"source-{account_id}-{abs(hash(text))}",
        message_db_id=None,
        outbound_message_id=None,
        direction="inbound",
        content_kind=content_kind,
        status="queued",
        risk_level="unknown",
        risk_categories=[],
        snapshot_text=text,
        media=media or {},
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

    fresh_db.moderation_llm_enabled = True
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
    fresh_db.moderation_llm_enabled = True
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


def test_worker_reuses_enqueued_rule_snapshot(fresh_db):
    """Worker 聚合 enqueue 时的规则快照，不用当前词表重判并改变结论。"""

    from app.db import (
        get_content_moderation_task,
        insert_content_moderation_result,
        list_content_moderation_results,
    )
    from app.platform.moderation.worker import run_once

    task = _queued_task(
        account_id="plum-rule-snapshot",
        app_id="plum",
        text="诱导未成年裸聊",
    )
    insert_content_moderation_result(
        task_id=task["id"],
        account_id=task["account_id"],
        reviewer_type="rule",
        engine="local_sensitive_terms",
        engine_version="enqueue_policy_v1",
        result_level="pass",
        reason="enqueue snapshot",
    )

    run_once(batch_size=1)
    updated = get_content_moderation_task(task_id=task["id"])
    results = list_content_moderation_results(task_id=task["id"])

    assert updated["status"] == "machine_passed"
    assert updated["risk_level"] == "pass"
    assert [result["reviewer_type"] for result in results].count("rule") == 1


def test_plum_worker_never_calls_image_or_llm_providers(monkeypatch, fresh_db):
    """全局 provider 全开时，Plum worker 仍只执行本地审核。"""

    from app.platform.moderation import worker

    fresh_db.moderation_image_safety_enabled = True
    fresh_db.moderation_llm_enabled = True
    task = _queued_task(
        account_id="plum-provider-gate",
        app_id="plum",
        text="normal plum image",
        content_kind="image",
        media={"url": "https://example.test/image.jpg"},
        metadata={"llm_review_selected": True},
    )

    def _unexpected(*args, **kwargs):
        raise AssertionError("Plum must not call external moderation providers")

    monkeypatch.setattr(worker.image_review, "review_image_task", _unexpected)
    monkeypatch.setattr(worker.llm_review, "review_text_with_llm", _unexpected)

    summary = worker.run_once(batch_size=1)

    assert summary["counts"]["machine_passed"] == 1
    assert summary["results"][0]["task_id"] == task["id"]


def test_domestic_worker_still_calls_enabled_image_provider(monkeypatch, fresh_db):
    """产品门控不能误伤国内产品已启用的图片审核。"""

    from app.platform.moderation import worker
    from app.platform.moderation.models import MachineReviewResult

    fresh_db.moderation_image_safety_enabled = True
    task = _queued_task(
        account_id="zhaoxi-image-provider",
        app_id="zhaoxi",
        text="",
        content_kind="image",
        media={"url": "https://example.test/image.jpg"},
    )
    calls = []

    def _review(claimed_task):
        calls.append(claimed_task["id"])
        return MachineReviewResult(
            reviewer_type="image_safety",
            engine="fake_image",
            level="pass",
        )

    monkeypatch.setattr(worker.image_review, "review_image_task", _review)

    worker.run_once(batch_size=1)

    assert calls == [task["id"]]


def test_worker_claims_stale_task_only_once(fresh_db):
    from app.db import claim_queued_content_moderation_tasks

    task = _queued_task(text="claim me once")
    first = claim_queued_content_moderation_tasks(batch_size=1, claim_timeout_seconds=300)
    second = claim_queued_content_moderation_tasks(batch_size=1, claim_timeout_seconds=300)

    assert [item["id"] for item in first] == [task["id"]]
    assert second == []
