"""Shared utilities for proactive message modules."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.db import list_channel_bindings_for_account


def format_reactivation_time(value: datetime) -> str:
    """主动消息时间戳统一格式化（北京 naive 时间字符串，秒粒度）。

    纯格式化、跨层复用（store / delivery / bridge / slots 都用），故落在契约层公共工具。
    """
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _select_route(account_id: str) -> Optional[Dict[str, Any]]:
    for binding in list_channel_bindings_for_account(account_id=account_id):
        to_user_id = _clean_text(binding.get("chat_id"))
        channel_account_id = _clean_text(binding.get("channel_account_id"))
        if not to_user_id or not channel_account_id:
            continue
        return {
            "channel_binding_id": binding["id"],
            "channel": binding["channel"],
            "channel_account_id": channel_account_id,
            "to_user_id": to_user_id,
            "session_key": binding.get("session_key"),
        }
    return None


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM output did not contain a JSON object")
    return json.loads(cleaned[start : end + 1])
