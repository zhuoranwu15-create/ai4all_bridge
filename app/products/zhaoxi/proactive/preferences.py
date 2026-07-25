"""账号级主动消息偏好（Phase 1 最小闭环 + Phase 2 频次/时段窗口）。

职责：把稀疏存储的账号 override 与全局配置 merge 成"有效设置"供 policy 与
scheduler 使用，并提供校验 + 落库 + 审计的 patch 入口。后端只做确定性校验，
自然语言意图由 LLM tool-use 表达，这里不做任何关键词/正则判断。

设计稿：/private/tmp/ai4all_proactive_message_settings_design.md
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import (
    get_proactive_message_settings_row,
    insert_proactive_message_setting_event,
    upsert_proactive_message_settings_row,
)
# 分类 registry 是单一数据源；此处 re-export 以保持历史 import 路径
# （main.py / serializers 仍 `from app.products.zhaoxi.proactive.preferences import PROACTIVE_FREQUENCY_BUCKETS`）。
from app.products.zhaoxi.proactive.contract.categories import (  # noqa: F401  (re-export)
    PROACTIVE_FREQUENCY_BUCKETS,
    PROACTIVE_SETTING_CATEGORIES,
)
from app.time_utils import beijing_now

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

_DB_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _freq_day_cap() -> int:
    try:
        return int(getattr(settings, "proactive_frequency_max_per_day_cap", 3) or 3)
    except (TypeError, ValueError):
        return 3


def _freq_week_cap() -> int:
    try:
        return int(getattr(settings, "proactive_frequency_max_per_week_cap", 14) or 14)
    except (TypeError, ValueError):
        return 14


def _hhmm_to_minutes(value: str) -> int:
    hour, minute = (int(p) for p in value.split(":", 1))
    return hour * 60 + minute


def _global_quiet_hours() -> Dict[str, Any]:
    """全局静默时段（来自 app.config.settings），作为未设置时的兜底。"""
    return {
        "enabled": True,
        "start": getattr(settings, "proactive_quiet_hours_start", "22:00"),
        "end": getattr(settings, "proactive_quiet_hours_end", "08:00"),
    }


def get_effective_proactive_message_settings(account_id: str) -> Dict[str, Any]:
    """返回账号有效主动消息设置：account override 缺失字段下沉到全局配置。

    无设置行或某字段为 NULL/空时，返回全局默认，从而保证"未设置=继承全局"，
    避免账号行的列默认值越权 override（P0-1）。
    """
    row = get_proactive_message_settings_row(account_id=account_id)

    master_enabled = bool(row["master_enabled"]) if row else True

    quiet_hours = None
    quiet_hours_is_user = False
    if row and isinstance(row.get("quiet_hours"), dict):
        quiet_hours = row["quiet_hours"]
        quiet_hours_is_user = True
    if not quiet_hours:
        quiet_hours = _global_quiet_hours()

    category_overrides = {}
    if row and isinstance(row.get("category_settings"), dict):
        category_overrides = row["category_settings"]
    categories: Dict[str, Dict[str, Any]] = {}
    for cat in PROACTIVE_SETTING_CATEGORIES:
        override = category_overrides.get(cat) if isinstance(category_overrides, dict) else None
        enabled = True
        if isinstance(override, dict) and "enabled" in override:
            enabled = bool(override["enabled"])
        categories[cat] = {"enabled": enabled}

    muted_until = row.get("muted_until") if row else None

    frequency = {}
    if row and isinstance(row.get("frequency"), dict):
        frequency = row["frequency"]
    allowed_windows = []
    if row and isinstance(row.get("allowed_windows"), list):
        allowed_windows = row["allowed_windows"]

    return {
        "master_enabled": master_enabled,
        "timezone": (row.get("timezone") if row else None) or "Asia/Shanghai",
        "quiet_hours": quiet_hours,
        "quiet_hours_is_user": quiet_hours_is_user,
        "categories": categories,
        "frequency": frequency,
        "allowed_windows": allowed_windows,
        "muted_until": muted_until,
    }


def is_category_enabled(effective: Dict[str, Any], category: str) -> bool:
    """某主动消息分类是否被用户关闭。未知/豁免分类返回 True（不拦截）。"""
    categories = effective.get("categories") or {}
    entry = categories.get(category)
    if isinstance(entry, dict) and "enabled" in entry:
        return bool(entry["enabled"])
    return True


def resolve_frequency_limits(effective: Dict[str, Any], bucket: str) -> Dict[str, Any]:
    """解析某桶的用户频次上限：bucket 专属 > default > 无。已 clamp 到系统硬上限。

    返回 {"max_per_day": int|None, "max_per_week": int|None, "day_is_user": bool}。
    值为 None 表示用户未设该项（调用方据此决定是否回落到全局日上限/不做周限）。
    """
    freq = effective.get("frequency") or {}
    bucket_cfg = freq.get(bucket) if isinstance(freq.get(bucket), dict) else {}
    default_cfg = freq.get("default") if isinstance(freq.get("default"), dict) else {}

    def _pick(field_name: str, cap: int):
        raw = bucket_cfg.get(field_name)
        if raw is None:
            raw = default_cfg.get(field_name)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return max(1, min(value, cap))

    max_per_day = _pick("max_per_day", _freq_day_cap())
    max_per_week = _pick("max_per_week", _freq_week_cap())
    return {
        "max_per_day": max_per_day,
        "max_per_week": max_per_week,
        "day_is_user": max_per_day is not None,
    }


def get_total_daily_limit(effective: Dict[str, Any]) -> Optional[int]:
    """返回用户设定的全局每日总量上限（total_per_day），未设则返回 None。已 clamp 到系统硬上限。"""
    freq = effective.get("frequency") or {}
    raw = freq.get("total_per_day")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, _freq_day_cap()))


def is_in_allowed_window(now: datetime, windows: Optional[List[Dict[str, Any]]]) -> bool:
    """now 是否落在任一允许窗口内。windows 为空 = 不限制（恒 True）。"""
    if not windows:
        return True
    weekday_label = WEEKDAYS[now.weekday()]
    current = now.hour * 60 + now.minute
    for window in windows:
        days = window.get("days") or []
        if weekday_label not in days:
            continue
        try:
            start = _hhmm_to_minutes(window["start"])
            end = _hhmm_to_minutes(window["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if start <= current < end:
            return True
    return False


def next_allowed_window_start(
    now: datetime,
    windows: Optional[List[Dict[str, Any]]],
) -> Optional[datetime]:
    """返回未来 7 天内最早的窗口起点（>= now）；无 windows 或找不到则 None。"""
    if not windows:
        return None
    best: Optional[datetime] = None
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        weekday_label = WEEKDAYS[day.weekday()]
        for window in windows:
            if weekday_label not in (window.get("days") or []):
                continue
            try:
                hour, minute = (int(p) for p in str(window["start"]).split(":", 1))
            except (KeyError, ValueError, TypeError):
                continue
            start_dt = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if start_dt >= now and (best is None or start_dt < best):
                best = start_dt
    return best


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate_hhmm(value: Any) -> str:
    text = str(value or "").strip()
    if ":" not in text:
        raise ValueError("时间必须是 HH:MM 格式")
    hour_text, minute_text = text.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        raise ValueError("时间必须是 HH:MM 格式")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间必须是 HH:MM 格式")
    return f"{hour:02d}:{minute:02d}"


def _parse_muted_until(value: Any) -> Optional[str]:
    """解析 muted_until。支持 'YYYY-MM-DD HH:MM[:SS]'；过去时间视为清除（None）。"""
    text = str(value or "").strip()
    if not text:
        return None
    parsed: Optional[datetime] = None
    for fmt in (_DB_TIME_FMT, "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("muted_until 必须是 'YYYY-MM-DD HH:MM:SS' 格式")
    now_naive = beijing_now().replace(tzinfo=None)
    if parsed <= now_naive:
        return None  # 过去时间 = 取消静默
    return parsed.strftime(_DB_TIME_FMT)


def _normalize_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    """校验并归一化 tool patch，返回只含被显式设置字段的干净 patch。

    account_id 一律忽略（只能来自调用方 ctx）。抛 ValueError 表示校验失败。
    """
    if not isinstance(patch, dict):
        raise ValueError("patch 必须是对象")
    clean: Dict[str, Any] = {}

    if "master_enabled" in patch and patch["master_enabled"] is not None:
        clean["master_enabled"] = bool(patch["master_enabled"])

    if "quiet_hours" in patch and patch["quiet_hours"] is not None:
        qh = patch["quiet_hours"]
        if not isinstance(qh, dict):
            raise ValueError("quiet_hours 必须是对象")
        enabled = bool(qh.get("enabled", True))
        start = _validate_hhmm(qh.get("start"))
        end = _validate_hhmm(qh.get("end"))
        clean["quiet_hours"] = {"enabled": enabled, "start": start, "end": end}

    if "category_updates" in patch and patch["category_updates"] is not None:
        updates = patch["category_updates"]
        if not isinstance(updates, dict):
            raise ValueError("category_updates 必须是对象")
        normalized_cats: Dict[str, Dict[str, Any]] = {}
        for cat, val in updates.items():
            if cat not in PROACTIVE_SETTING_CATEGORIES:
                raise ValueError(f"未知主动消息分类: {cat}")
            if isinstance(val, dict):
                enabled = bool(val.get("enabled", True))
            else:
                enabled = bool(val)
            normalized_cats[cat] = {"enabled": enabled}
        if normalized_cats:
            clean["category_updates"] = normalized_cats

    if "frequency" in patch and patch["frequency"] is not None:
        freq = patch["frequency"]
        if not isinstance(freq, dict):
            raise ValueError("frequency 必须是对象")
        day_cap = _freq_day_cap()
        week_cap = _freq_week_cap()
        normalized_freq: Dict[str, Any] = {}
        for key, cfg in freq.items():
            # total_per_day：全局每日总量上限，整数，不是分组桶
            if key == "total_per_day":
                try:
                    value = int(cfg)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    raise ValueError("total_per_day 必须是整数")
                if value < 1:
                    raise ValueError("total_per_day 必须 >= 1")
                normalized_freq["total_per_day"] = min(value, day_cap)
                continue
            if key != "default" and key not in PROACTIVE_FREQUENCY_BUCKETS:
                raise ValueError(f"未知频次分组: {key}")
            if not isinstance(cfg, dict):
                raise ValueError("频次配置项必须是对象")
            entry: Dict[str, int] = {}
            for field_name, cap in (("max_per_day", day_cap), ("max_per_week", week_cap)):
                if field_name in cfg and cfg[field_name] is not None:
                    try:
                        value = int(cfg[field_name])
                    except (TypeError, ValueError):
                        raise ValueError(f"{field_name} 必须是整数")
                    if value < 1:
                        raise ValueError(f"{field_name} 必须 >= 1")
                    entry[field_name] = min(value, cap)  # 超系统硬上限即封顶
            if entry:
                normalized_freq[key] = entry
        # 空 dict 也允许（清除全部频次 override）
        clean["frequency"] = normalized_freq

    if "allowed_windows" in patch and patch["allowed_windows"] is not None:
        windows = patch["allowed_windows"]
        if not isinstance(windows, list):
            raise ValueError("allowed_windows 必须是数组")
        if len(windows) > 7:
            raise ValueError("最多 7 个时段窗口")
        normalized_windows: List[Dict[str, Any]] = []
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError("时段窗口必须是对象")
            days = window.get("days")
            if not isinstance(days, list) or not days:
                raise ValueError("时段窗口 days 不能为空")
            norm_days: List[str] = []
            for day in days:
                day_label = str(day).strip().upper()
                if day_label not in WEEKDAYS:
                    raise ValueError(f"未知星期: {day}")
                if day_label not in norm_days:
                    norm_days.append(day_label)
            start = _validate_hhmm(window.get("start"))
            end = _validate_hhmm(window.get("end"))
            if start >= end:
                raise ValueError("时段窗口 start 必须早于 end（暂不支持跨午夜）")
            normalized_windows.append({"days": norm_days, "start": start, "end": end})
        # 空数组也允许（清除时段限制）
        clean["allowed_windows"] = normalized_windows

    if "muted_until" in patch:
        clean["muted_until"] = _parse_muted_until(patch["muted_until"])

    return clean


# ---------------------------------------------------------------------------
# 应用 patch
# ---------------------------------------------------------------------------

def _diff_changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """对比 before/after 有效视图，返回点号字段名列表。"""
    changed: List[str] = []
    if before.get("master_enabled") != after.get("master_enabled"):
        changed.append("master_enabled")
    if before.get("quiet_hours") != after.get("quiet_hours"):
        changed.append("quiet_hours")
    if before.get("muted_until") != after.get("muted_until"):
        changed.append("muted_until")
    if before.get("frequency") != after.get("frequency"):
        changed.append("frequency")
    if before.get("allowed_windows") != after.get("allowed_windows"):
        changed.append("allowed_windows")
    before_cats = before.get("categories") or {}
    after_cats = after.get("categories") or {}
    for cat in PROACTIVE_SETTING_CATEGORIES:
        if (before_cats.get(cat) or {}).get("enabled") != (after_cats.get(cat) or {}).get("enabled"):
            changed.append(f"categories.{cat}.enabled")
    return changed


def apply_proactive_message_settings_patch(
    *,
    account_id: str,
    patch: Dict[str, Any],
    source: str,
    tool_invocation_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """校验 → 读 before → upsert → 写审计，返回 after 有效视图与 changed_fields。

    抛 ValueError 表示校验失败（调用方应转成 {"error": ...} 返回给 LLM）。
    """
    if not str(account_id or "").strip():
        raise ValueError("account_id is required")

    clean = _normalize_patch(patch)

    before_effective = get_effective_proactive_message_settings(account_id)
    before_row = get_proactive_message_settings_row(account_id=account_id)

    # 组装 upsert kwargs：未出现的字段不传，db 层默认 _UNSET 即保持原值不动。
    upsert_kwargs: Dict[str, Any] = {"account_id": account_id}
    if "master_enabled" in clean:
        upsert_kwargs["master_enabled"] = clean["master_enabled"]
    if "quiet_hours" in clean:
        upsert_kwargs["quiet_hours"] = clean["quiet_hours"]
    if "muted_until" in clean:
        # None 表示显式清除（写 NULL）
        upsert_kwargs["muted_until"] = clean["muted_until"]
    if "category_updates" in clean:
        current_cats = {}
        if before_row and isinstance(before_row.get("category_settings"), dict):
            current_cats = dict(before_row["category_settings"])
        current_cats.update(clean["category_updates"])
        upsert_kwargs["category_settings"] = current_cats
    if "frequency" in clean:
        # 按桶 merge：只覆盖用户本次提到的桶，保留其它桶已有设置。
        # 本次显式给空 dict（清除）时直接替换为空。
        if clean["frequency"]:
            current_freq = {}
            if before_row and isinstance(before_row.get("frequency"), dict):
                current_freq = dict(before_row["frequency"])
            current_freq.update(clean["frequency"])
            upsert_kwargs["frequency"] = current_freq
        else:
            upsert_kwargs["frequency"] = {}
    if "allowed_windows" in clean:
        # 时段窗口整体替换（用户每次给出完整窗口集）。
        upsert_kwargs["allowed_windows"] = clean["allowed_windows"]

    # 只有 account_id 没有任何变更字段时，仍然 upsert 以确保行存在 + 记录审计
    upsert_proactive_message_settings_row(**upsert_kwargs)

    after_effective = get_effective_proactive_message_settings(account_id)
    changed_fields = _diff_changed_fields(before_effective, after_effective)

    insert_proactive_message_setting_event(
        account_id=account_id,
        source=source,
        tool_invocation_id=tool_invocation_id,
        previous_settings=before_effective,
        patch=clean,
        next_settings=after_effective,
        reason=reason,
    )

    return {
        "settings": after_effective,
        "changed_fields": changed_fields,
        "previous": before_effective,
    }
