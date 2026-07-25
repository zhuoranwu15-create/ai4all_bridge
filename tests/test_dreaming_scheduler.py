import asyncio
from datetime import datetime


def test_dreaming_scheduler_refreshes_idle_heartbeat_during_long_wait(monkeypatch):
    from app.products.zhaoxi.jobs.dreaming import scheduler as module

    scheduler = module.DreamingScheduler(batch_size=1, start_hour=4)
    now_values = iter(
        [
            datetime(2026, 6, 7, 4, 10, 0),
            datetime(2026, 6, 8, 3, 59, 50),
        ]
    )
    timeouts = []
    heartbeats = []

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        timeouts.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(module, "beijing_now", lambda: next(now_values))
    monkeypatch.setattr(module.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(
        scheduler,
        "_record_heartbeat",
        lambda *, status, error=None: heartbeats.append((status, error)),
    )

    asyncio.run(scheduler._sleep_until_next_window(heartbeat_status="idle"))

    assert timeouts == [module._IDLE_HEARTBEAT_INTERVAL_SECONDS, 10.0]
    assert heartbeats == [("idle", None)]


def test_dreaming_scheduler_keeps_error_status_during_wait(monkeypatch):
    from app.products.zhaoxi.jobs.dreaming import scheduler as module

    scheduler = module.DreamingScheduler(batch_size=1, start_hour=4)
    now_values = iter(
        [
            datetime(2026, 6, 7, 4, 10, 0),
            datetime(2026, 6, 8, 3, 59, 50),
        ]
    )
    heartbeats = []

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(module, "beijing_now", lambda: next(now_values))
    monkeypatch.setattr(module.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(
        scheduler,
        "_record_heartbeat",
        lambda *, status, error=None: heartbeats.append((status, error)),
    )

    asyncio.run(
        scheduler._sleep_until_next_window(
            heartbeat_status="error",
            heartbeat_error="boom",
        )
    )

    assert heartbeats == [("error", "boom")]


def test_dreaming_scheduler_injects_sink_and_runs_compact_batch(monkeypatch):
    from app.products.zhaoxi.jobs.dreaming import scheduler as module

    marker_sink = object()
    scan_calls = []
    compact_calls = []

    def fake_scan(**kwargs):
        scan_calls.append(kwargs)
        return {"status": "ok", "scanned": 0, "results": []}

    def fake_compact(**kwargs):
        compact_calls.append(kwargs)
        return {
            "scanned": 1,
            "merged_groups": 1,
            "superseded_facts": 2,
            "next_cursor": "uni-next",
            "results": [],
        }

    monkeypatch.setattr(module, "run_daily_dreaming_scan", fake_scan)
    scheduler = module.DreamingScheduler(
        batch_size=7,
        memory_sink=marker_sink,
        memory_compactor=fake_compact,
    )
    result = asyncio.run(scheduler.run_once(now=datetime(2026, 7, 22, 4, 5)))

    assert scan_calls == [
        {
            "now": datetime(2026, 7, 22, 4, 5),
            "limit": 7,
            "node_id": None,
            "memory_sink": marker_sink,
        }
    ]
    assert compact_calls == [{"limit": 7, "after_universe_id": None}]
    assert result["memory_compaction"]["merged_groups"] == 1
    assert scheduler.status()["memory_compact_cursor"] == "uni-next"
