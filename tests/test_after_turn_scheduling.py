"""after-turn 后台调度的 shutdown 竞态防护（_schedule_on_loop）。

回归目标：turn 终结阶段把 after-turn 工作（记忆写入 / 关系状态 / 滚动摘要 / TDAI capture）
线程安全地挂到后台事件循环。若 loop 已在关停期被关闭，旧代码仅判 `is not None` → call_soon_threadsafe
对已关闭 loop 抛 RuntimeError，冒泡打断主回复。修复后应静默跳过（记 warning）、不抛、且不泄漏协程。
"""
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.turn_service as turn_service
from app.turn_service import _dispatch_after_turn, _schedule_on_loop


async def _noop():  # pragma: no cover - body never runs, 用于构造协程对象
    return None


def test_closed_loop_skips_without_raising():
    """loop 已关闭：返回 False、不抛、协程被 close（无 'never awaited' 泄漏）。"""
    loop = asyncio.new_event_loop()
    loop.close()
    coro = _noop()

    result = _schedule_on_loop(loop, coro)

    assert result is False
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_mid_schedule_close_race_is_caught():
    """is_closed() 通过但调度瞬间 loop 被关闭（RuntimeError）：吞掉、返回 False、协程被 close。"""
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
    coro = _noop()

    result = _schedule_on_loop(loop, coro)

    assert result is False
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


def test_open_loop_schedules_task():
    """loop 正常：经 call_soon_threadsafe(create_task, coro) 挂上，返回 True。"""
    loop = MagicMock()
    loop.is_closed.return_value = False
    coro = _noop()

    result = _schedule_on_loop(loop, coro)

    assert result is True
    loop.call_soon_threadsafe.assert_called_once_with(loop.create_task, coro)
    # 协程交给 loop 调度、未被 close（仍待 create_task 执行）。
    assert inspect.getcoroutinestate(coro) != inspect.CORO_CLOSED
    coro.close()  # 清理：MagicMock 不会真的 await 它


# ---------- _dispatch_after_turn：hook 注册表派发 ----------
def _atx():
    return SimpleNamespace(account_id="acct-x")


def test_dispatch_none_loop_warns_and_records_timing(monkeypatch):
    """无后台 loop：整体跳过（不调度任何 hook），但仍记 after_turn_enqueue_ms timing。"""
    scheduled = []
    monkeypatch.setattr(turn_service, "_schedule_on_loop", lambda loop, coro: scheduled.append(coro))
    timings = {}

    _dispatch_after_turn(_atx(), background_loop=None, timings=timings, started_at=0.0)

    assert scheduled == []
    assert "after_turn_enqueue_ms" in timings


def test_dispatch_isolates_hook_build_failure(monkeypatch):
    """单个 hook 构造抛异常被隔离：其余 hook 仍照常挂载，timing 仍记录。"""
    good_coro = object()

    def _bad_hook(atx):
        raise RuntimeError("boom")

    def _good_hook(atx):
        return good_coro

    monkeypatch.setattr(
        turn_service,
        "_AFTER_TURN_HOOKS",
        [("bad", _bad_hook), ("good", _good_hook)],
    )
    scheduled = []
    monkeypatch.setattr(turn_service, "_schedule_on_loop", lambda loop, coro: scheduled.append(coro))
    timings = {}

    _dispatch_after_turn(_atx(), background_loop=MagicMock(), timings=timings, started_at=0.0)

    assert scheduled == [good_coro]  # 坏 hook 不阻断好 hook
    assert "after_turn_enqueue_ms" in timings


def test_dispatch_skips_hooks_returning_none(monkeypatch):
    """hook 返回 None（本轮不适用，如开关关闭）时跳过、不调度。"""
    monkeypatch.setattr(
        turn_service,
        "_AFTER_TURN_HOOKS",
        [("skip", lambda atx: None), ("run", lambda atx: "coro-run")],
    )
    scheduled = []
    monkeypatch.setattr(turn_service, "_schedule_on_loop", lambda loop, coro: scheduled.append(coro))

    _dispatch_after_turn(_atx(), background_loop=MagicMock(), timings={}, started_at=0.0)

    assert scheduled == ["coro-run"]
