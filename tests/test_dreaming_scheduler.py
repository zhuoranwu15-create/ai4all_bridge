import asyncio
from datetime import datetime


def test_dreaming_scheduler_refreshes_idle_heartbeat_during_long_wait(monkeypatch):
    from app import dreaming_scheduler as module

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
    from app import dreaming_scheduler as module

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
