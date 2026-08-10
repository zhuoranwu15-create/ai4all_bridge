import pytest


def _create(turn_id: str, idempotency_key: str, *, session_id: int = 1):
    from app.db import create_runtime_turn_run

    return create_runtime_turn_run(
        turn_id=turn_id,
        app_id="plum",
        account_id="account-1",
        session_id=session_id,
        client_message_id=f"client-{turn_id}",
        idempotency_key=idempotency_key,
        provider_id="provider-1",
        model_ref="model-1",
    )


def test_runtime_turn_run_idempotency_and_active_session_guard(fresh_db):
    from app.db import ActiveRuntimeTurnError, finish_runtime_turn_run

    first, created = _create("turn-1", "idem-1")
    assert created is True
    assert first["status"] == "accepted"

    replay, created = _create("turn-retry", "idem-1")
    assert created is False
    assert replay["id"] == "turn-1"

    with pytest.raises(ActiveRuntimeTurnError, match="runtime_turn_active"):
        _create("turn-2", "idem-2")

    assert finish_runtime_turn_run(turn_id="turn-1", status="completed", finish_reason="stop")
    second, created = _create("turn-2", "idem-2")
    assert created is True
    assert second["status"] == "accepted"


def test_runtime_turn_run_first_delta_and_terminal_are_single_transition(fresh_db):
    from app.db import (
        finish_runtime_turn_run,
        get_runtime_turn_run,
        mark_runtime_turn_first_delta,
        mark_runtime_turn_running,
    )

    _create("turn-1", "idem-1")
    assert mark_runtime_turn_running("turn-1")
    assert mark_runtime_turn_first_delta("turn-1")
    assert finish_runtime_turn_run(
        turn_id="turn-1",
        status="cancelled",
        assistant_message_id="reply-1",
        finish_reason="client_cancelled",
    )
    assert not finish_runtime_turn_run(turn_id="turn-1", status="failed", error_code="late")

    run = get_runtime_turn_run("turn-1")
    assert run["status"] == "cancelled"
    assert run["first_delta_at"] is not None
    assert run["assistant_message_id"] == "reply-1"


def test_runtime_turn_cancel_request_wins_before_first_delta(fresh_db):
    from app.db import (
        get_runtime_turn_run,
        mark_runtime_turn_first_delta,
        request_runtime_turn_cancel,
        runtime_turn_cancel_requested,
    )

    _create("turn-1", "idem-1")
    assert request_runtime_turn_cancel(
        turn_id="turn-1", app_id="plum", account_id="account-1", session_id=1
    )
    assert runtime_turn_cancel_requested("turn-1")
    assert not mark_runtime_turn_first_delta("turn-1")
    assert get_runtime_turn_run("turn-1")["first_delta_at"] is None


def test_runtime_turn_cancel_request_after_first_delta_preserves_delivery(fresh_db):
    from app.db import (
        get_runtime_turn_run,
        mark_runtime_turn_first_delta,
        request_runtime_turn_cancel,
    )

    _create("turn-1", "idem-1")
    assert mark_runtime_turn_first_delta("turn-1")
    assert request_runtime_turn_cancel(
        turn_id="turn-1", app_id="plum", account_id="account-1", session_id=1
    )
    run = get_runtime_turn_run("turn-1")
    assert run["first_delta_at"] is not None
    assert run["cancel_requested_at"] is not None


def test_reclaim_stale_runtime_turn_runs(fresh_db):
    from app.db import connect, get_runtime_turn_run, reclaim_stale_runtime_turn_runs

    _create("turn-1", "idem-1")
    with connect() as conn:
        conn.execute(
            "UPDATE runtime_turn_runs SET updated_at='2000-01-01 00:00:00' WHERE id='turn-1'"
        )

    reclaimed = reclaim_stale_runtime_turn_runs(ttl_seconds=600)

    assert [run["id"] for run in reclaimed] == ["turn-1"]
    assert get_runtime_turn_run("turn-1")["status"] == "abandoned"


def test_reclaim_stale_runtime_turn_runs_can_scope_one_session(fresh_db):
    from app.db import connect, get_runtime_turn_run, reclaim_stale_runtime_turn_runs

    _create("turn-1", "idem-1", session_id=1)
    _create("turn-2", "idem-2", session_id=2)
    with connect() as conn:
        conn.execute(
            "UPDATE runtime_turn_runs SET updated_at='2000-01-01 00:00:00'"
        )

    reclaimed = reclaim_stale_runtime_turn_runs(
        ttl_seconds=600,
        app_id="plum",
        account_id="account-1",
        session_id=1,
    )

    assert [run["id"] for run in reclaimed] == ["turn-1"]
    assert get_runtime_turn_run("turn-1")["status"] == "abandoned"
    assert get_runtime_turn_run("turn-2")["status"] == "accepted"
