"""产品级内容审核策略。

审核的 provider、词表与开关此前只有一组全局 settings。朝夕/鸣蝉面向国内、Plum 面向海外，
而阿里云文本审核 PLUS 是境内 endpoint + 中文场景码，对海外流量既不适用也不合规，
因此把「哪个产品用哪套审核」变成显式表，而不是继续靠一组全局布尔。

未知 app_id 刻意**不抛异常**：turn engine 对入站筛查整体 try/except，异常时
``inbound_screen = None`` 并放行本轮（见 ``app/agent_runtime/turns/service.py``），
所以抛异常在这里等价于 fail-open。真正的 fail closed 是回落到最保守策略——
管线照常记录、本地规则照常跑、但不调用任何外部 provider。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PLUM_APP_ID,
    ZHAOXI_APP_ID,
)
from app.config import settings

logger = logging.getLogger("ai4all.moderation.product_policy")

# Plum 面向海外，中文敏感词表不适用；这里是占位词表，选定海外 provider 后再补齐。
PLUM_SENSITIVE_TERMS_PATH = "data/moderation/sensitive_terms_plum.json"


@dataclass(frozen=True)
class ModerationProductPolicy:
    """一个产品在内容审核链路上的完整开关集合。

    ``sensitive_terms_path`` 为 None 表示沿用全局 ``moderation_sensitive_terms_path``。
    """

    app_id: str
    enabled: bool
    sync_guard_enabled: bool
    aliyun_inbound_sync_enabled: bool
    llm_enabled: bool
    sensitive_terms_path: Optional[str] = None


def _global_enabled() -> bool:
    return bool(getattr(settings, "moderation_enabled", True))


def _global_sync_guard_enabled() -> bool:
    return bool(getattr(settings, "moderation_sync_guard_enabled", True))


def _global_llm_enabled() -> bool:
    return bool(getattr(settings, "moderation_llm_enabled", False))


def _global_aliyun_inbound_sync_enabled() -> bool:
    """入站是否走阿里云同步筛查：总开关与入站同步开关同时为真。"""

    return bool(getattr(settings, "moderation_aliyun_enabled", False)) and bool(
        getattr(settings, "moderation_aliyun_inbound_sync_enabled", True)
    )


def _domestic_policy(app_id: str) -> ModerationProductPolicy:
    """国内产品：全部读回既有全局开关，行为与产品级策略引入前逐项一致。"""

    return ModerationProductPolicy(
        app_id=app_id,
        enabled=_global_enabled(),
        sync_guard_enabled=_global_sync_guard_enabled(),
        aliyun_inbound_sync_enabled=_global_aliyun_inbound_sync_enabled(),
        llm_enabled=_global_llm_enabled(),
        sensitive_terms_path=None,
    )


def _plum_policy(app_id: str) -> ModerationProductPolicy:
    """Plum（海外）：管线与任务记录照常，但绝不调用阿里云。

    ``aliyun_inbound_sync_enabled`` 写死 False 而非读环境变量——把海外流量送进境内
    审核既是合规问题也是延迟问题，不接受被一处 ``.env`` 笔误打开。选定海外 provider
    后在此处接入，届时管线里已有可回溯的存量任务。
    """

    return ModerationProductPolicy(
        app_id=app_id,
        enabled=_global_enabled(),
        sync_guard_enabled=_global_sync_guard_enabled(),
        aliyun_inbound_sync_enabled=False,
        llm_enabled=_global_llm_enabled(),
        sensitive_terms_path=PLUM_SENSITIVE_TERMS_PATH,
    )


def _conservative_policy(app_id: str) -> ModerationProductPolicy:
    """未登记产品的兜底：记录 + 本地规则，不调任何外部 provider。"""

    return ModerationProductPolicy(
        app_id=app_id,
        enabled=True,
        sync_guard_enabled=True,
        aliyun_inbound_sync_enabled=False,
        llm_enabled=False,
        sensitive_terms_path=None,
    )


# 显式产品表。新增产品必须在此登记，否则按 _conservative_policy 处理并告警。
_POLICY_BUILDERS: Dict[str, Callable[[str], ModerationProductPolicy]] = {
    ZHAOXI_APP_ID: _domestic_policy,
    MINGCHAN_APP_ID: _domestic_policy,
    PLUM_APP_ID: _plum_policy,
    # 跨产品隔离测试用的注入产品，按国内档处理以复用既有测试断言。
    "test_product": _domestic_policy,
}


def resolve_moderation_policy(app_id: str) -> ModerationProductPolicy:
    """按 app_id 解析审核策略；未登记产品回落保守档并告警。"""

    cleaned = str(app_id or "").strip()
    builder = _POLICY_BUILDERS.get(cleaned)
    if builder is None:
        logger.warning(
            "moderation policy missing for app_id=%s; falling back to conservative policy",
            cleaned or "<empty>",
        )
        return _conservative_policy(cleaned)
    return builder(cleaned)


__all__ = [
    "ModerationProductPolicy",
    "PLUM_SENSITIVE_TERMS_PATH",
    "resolve_moderation_policy",
]
