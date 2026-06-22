"""主动消息设定工具 handler（Phase 1）。

get：返回当前账号有效主动消息设置 + 中文摘要。
update：校验并落库一次偏好变更（带审计），返回结果摘要。

账号隔离：account_id 一律取自 ctx，patch/args 里的 account_id 一律忽略。
本工具只影响系统主动触达，绝不影响用户提醒。
"""
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

from app.proactive.settings import (
    apply_proactive_message_settings_patch,
    get_effective_proactive_message_settings,
)

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.proactive_settings_handlers")

from app.proactive.categories import CATEGORY_LABELS as _CATEGORY_LABELS

_UPDATE_ARG_KEYS = {
    "master_enabled",
    "category_updates",
    "quiet_hours",
    "muted_until",
    "frequency",
    "allowed_windows",
    "total_per_day",
    "reason",
    "account_id",
}


def _summarize(effective: Dict[str, Any]) -> str:
    """把有效设置渲染成一句给 LLM 复述用的中文摘要。"""
    if not effective.get("master_enabled", True):
        return "主动消息已整体关闭（用户提醒不受影响）。"

    parts = ["主动消息开启"]

    disabled = [
        _CATEGORY_LABELS.get(cat, cat)
        for cat, entry in (effective.get("categories") or {}).items()
        if not (entry or {}).get("enabled", True)
    ]
    if disabled:
        parts.append("已关闭：" + "、".join(disabled))

    windows = effective.get("allowed_windows") or []
    if windows:
        rendered = "、".join(
            f"{'/'.join(w.get('days') or [])} {w.get('start')}-{w.get('end')}" for w in windows
        )
        parts.append(f"仅在 {rendered} 推送")

    quiet = effective.get("quiet_hours") or {}
    if quiet.get("enabled", True):
        parts.append(f"静默时段 {quiet.get('start')}-{quiet.get('end')}")

    freq = effective.get("frequency") or {}
    if freq:
        freq_bits = []
        total_per_day = freq.get("total_per_day")
        if total_per_day is not None:
            freq_bits.append(f"每天总计最多{total_per_day}条")
        for bucket, cfg in freq.items():
            if bucket == "total_per_day" or not isinstance(cfg, dict):
                continue
            limits = []
            if cfg.get("max_per_day") is not None:
                limits.append(f"每天{cfg['max_per_day']}次")
            if cfg.get("max_per_week") is not None:
                limits.append(f"每周{cfg['max_per_week']}次")
            if limits:
                freq_bits.append(f"{bucket}:{'/'.join(limits)}")
        if freq_bits:
            parts.append("频次 " + "，".join(freq_bits) + "（受系统上限约束，提醒不计入总量）")

    muted_until = str(effective.get("muted_until") or "").strip()
    if muted_until:
        parts.append(f"临时静默至 {muted_until}")

    return "；".join(parts) + "。提醒不受影响。"


def handle_get_proactive_message_settings(args: dict, ctx: "TurnContext") -> dict:
    """返回当前账号的有效主动消息设置与中文摘要。只读。"""
    effective = get_effective_proactive_message_settings(ctx.account_id)
    return {
        "status": "ok",
        "settings": effective,
        "summary": _summarize(effective),
    }


def handle_update_proactive_message_settings(
    args: dict,
    ctx: "TurnContext",
    *,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    """校验并应用一次主动消息偏好变更，写审计。校验失败返回 {"error": ...}。"""
    unknown_keys = sorted(str(key) for key in args.keys() if key not in _UPDATE_ARG_KEYS)
    if unknown_keys:
        return {"error": "不支持的设置字段: " + "、".join(unknown_keys)}

    patch = {
        key: args[key]
        for key in (
            "master_enabled",
            "category_updates",
            "quiet_hours",
            "muted_until",
            "frequency",
            "allowed_windows",
        )
        if key in args
    }
    # 顶层 total_per_day 参数 → 合并进 frequency.total_per_day（更自然的 LLM 调用形式）
    if "total_per_day" in args and args["total_per_day"] is not None:
        freq = dict(patch.get("frequency") or {})
        freq["total_per_day"] = args["total_per_day"]
        patch["frequency"] = freq
    if not patch:
        return {"error": "没有可更新的设置字段"}

    try:
        result = apply_proactive_message_settings_patch(
            account_id=ctx.account_id,
            patch=patch,
            source="tool",
            tool_invocation_id=tool_invocation_id,
            reason=str(args.get("reason") or "").strip() or None,
        )
    except ValueError as err:
        return {"error": str(err)}

    effective = result["settings"]
    return {
        "status": "updated",
        "settings": effective,
        "changed_fields": result["changed_fields"],
        "summary": _summarize(effective),
    }
