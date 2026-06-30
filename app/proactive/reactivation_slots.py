"""Reactivation 发送 slot 调度 + 时间格式化（纯函数，无 DB 写）。

从 reactivation.py 抽出：slot 解析、jitter、窗口感知排期等。零行为变更迁移。
reactivation.py 仍从本模块 re-import，对外 `app.proactive.reactivation.next_reactivation_slot` 等路径不变。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.proactive.settings import (
    get_effective_proactive_message_settings,
    is_in_allowed_window,
    next_allowed_window_start,
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def format_reactivation_time(value: datetime) -> str:
    """Format reactivation timestamps consistently with proactive state metadata."""
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

def _parse_send_slots(value: Optional[str] = None) -> List[str]:
    raw = value if value is not None else getattr(settings, "reactivation_send_slots", "12:15,18:15,21:05")
    slots: List[str] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError:
            continue
        slots.append(text)
    return slots or ["12:15", "18:15", "21:05"]

def _slot_datetime(day: datetime, slot: str) -> datetime:
    hour, minute = [int(part) for part in slot.split(":", 1)]
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

def _apply_send_jitter(scheduled: datetime) -> datetime:
    """Add a small forward random offset to a slot time so sends spread out.

    Forward-only (never before the slot). Configurable via
    reactivation_send_jitter_min/max_seconds; 0/0 disables (used in tests).
    """
    try:
        low = int(getattr(settings, "reactivation_send_jitter_min_seconds", 60) or 0)
        high = int(getattr(settings, "reactivation_send_jitter_max_seconds", 120) or 0)
    except (TypeError, ValueError):
        low, high = 60, 120
    low = max(low, 0)
    high = max(high, 0)
    if high <= 0:
        return scheduled
    if low > high:
        low = high
    return scheduled + timedelta(seconds=random.randint(low, high))

def _account_allowed_windows(account_id: str) -> List[Dict[str, Any]]:
    """读取账号的允许推送时段窗口；读取失败时返回空（=不限制），不阻断调度。"""
    try:
        eff = get_effective_proactive_message_settings(account_id)
        windows = eff.get("allowed_windows") or []
        return windows if isinstance(windows, list) else []
    except Exception:  # pragma: no cover - 调度不应因设置读取失败而崩
        return []

def next_reactivation_slot(
    *,
    now: datetime,
    after_slot: Optional[str] = None,
    allowed_windows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Return the next configured reactivation send slot at or after now.

    allowed_windows 非空时，发送时间必须落在用户窗口内：在未来 8 天里取第一个
    >=now 且命中窗口的固定 slot；若都不命中，则 snap 到下一个窗口起点
    （scheduled_slot="window_start"），避免固定 slot 落不进窗口导致永不发送。
    """
    slots = _parse_send_slots()
    start_index = 0
    if after_slot in slots:
        start_index = slots.index(after_slot) + 1

    if not allowed_windows:
        for index, slot in enumerate(slots[start_index:], start=start_index):
            scheduled = _slot_datetime(now, slot)
            if scheduled >= now:
                return {
                    "scheduled_slot": f"slot_{index + 1}",
                    "scheduled_at": format_reactivation_time(_apply_send_jitter(scheduled)),
                }
        first = _slot_datetime(now + timedelta(days=1), slots[0])
        return {
            "scheduled_slot": "slot_1",
            "scheduled_at": format_reactivation_time(_apply_send_jitter(first)),
        }

    # 窗口感知：第一天尊重 after_slot 起点，后续天数遍历全部 slot。
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        base_index = start_index if offset == 0 else 0
        day_slots = slots[base_index:]
        for j, slot in enumerate(day_slots, start=base_index):
            scheduled = _slot_datetime(day, slot)
            if scheduled >= now and is_in_allowed_window(scheduled, allowed_windows):
                return {
                    "scheduled_slot": f"slot_{j + 1}",
                    "scheduled_at": format_reactivation_time(_apply_send_jitter(scheduled)),
                }
    window_start = next_allowed_window_start(now, allowed_windows)
    if window_start is not None:
        return {
            "scheduled_slot": "window_start",
            "scheduled_at": format_reactivation_time(_apply_send_jitter(window_start)),
        }
    first = _slot_datetime(now + timedelta(days=1), slots[0])
    return {
        "scheduled_slot": "slot_1",
        "scheduled_at": format_reactivation_time(_apply_send_jitter(first)),
    }

def _next_slot_after(
    candidate: Dict[str, Any],
    *,
    now: datetime,
    allowed_windows: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, str]]:
    # 有窗口时：直接取下一个 >=now 且落窗的发送时间（必要时 snap 到窗口起点）。
    if allowed_windows:
        return next_reactivation_slot(now=now, allowed_windows=allowed_windows)

    slots = _parse_send_slots()
    current_slot = _clean_text(candidate.get("scheduled_slot"))
    if current_slot.startswith("slot_"):
        try:
            current_index = int(current_slot.removeprefix("slot_")) - 1
        except ValueError:
            current_index = -1
    else:
        current_index = -1
    next_index = current_index + 1
    if next_index >= len(slots):
        return None
    slot = slots[next_index]
    scheduled = _slot_datetime(now, slot)
    if scheduled < now:
        scheduled = _slot_datetime(now + timedelta(days=1), slot)
    return {
        "scheduled_slot": f"slot_{next_index + 1}",
        "scheduled_at": format_reactivation_time(_apply_send_jitter(scheduled)),
    }

def _with_default_schedule(
    candidate: Dict[str, Any],
    *,
    now: datetime,
    allowed_windows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    next_candidate = dict(candidate)
    if not _clean_text(next_candidate.get("scheduled_at")):
        next_candidate.update(next_reactivation_slot(now=now, allowed_windows=allowed_windows))
    return next_candidate
