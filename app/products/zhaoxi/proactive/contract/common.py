"""Shared utilities for proactive message modules."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.platform.channels import get_channel_capability
from app.platform.channels import CHANNEL_APP
from app.config import settings
from app.db import list_channel_bindings_for_account
from app.products.zhaoxi.infrastructure.app_inbox import AppInboxAdapter
from app.products.zhaoxi.infrastructure.repositories.companion_world import human_level_app_route


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
    app_inbox_route: Optional[Dict[str, Any]] = None
    for binding in list_channel_bindings_for_account(account_id=account_id):
        if (
            binding.get("channel") == CHANNEL_APP
            and bool(getattr(settings, "companion_world_app_inbox_enabled", False))
            and AppInboxAdapter().can_deliver(account_id)
        ):
            app_inbox_route = {
                "channel_binding_id": binding["id"],
                "channel": CHANNEL_APP,
                "channel_account_id": binding.get("channel_account_id"),
                "to_user_id": (
                    _clean_text(binding.get("chat_id"))
                    or _clean_text(binding.get("sender_id"))
                    or account_id
                ),
                "session_key": binding.get("session_key"),
                "delivery": "app_inbox",
            }
            continue
        # 主动路由能力过滤（§8.3，原则一硬需求）：只选可被主动消息投递的渠道。
        # bindings 按 last_seen_at DESC 排序（accounts.py），Web turn 会把 web binding
        # 顶到微信前——若不过滤，现有正常工作的微信主动路由会因账号多一条 web 足迹而
        # 静默改道（对微信的回归）。V1 只有 openclaw-weixin 的 supports_proactive=True，
        # 故对纯微信账号行为等价现状。
        if not get_channel_capability(binding.get("channel", "")).supports_proactive:
            continue
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
    # 真人级 App-only 不依赖某条 channel_binding：收件箱按 platform owner 拉取。
    # 这里只为当前确定性 speaker 合成 route；双 flag/微信优先均由 resolver 再校验。
    return app_inbox_route or human_level_app_route(account_id)


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
