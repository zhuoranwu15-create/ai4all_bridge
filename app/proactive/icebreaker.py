"""主动破冰话术核心模块。

提供：
  pick_icebreaker_script(account_id, now=None) -> Optional[Dict]
  dispatch_icebreaker(account_id, now=None)    -> Dict
  dispatch_due_icebreakers(now, limit, node_id) -> List[Dict]

选取规则（v1，确定性 + 少量随机扰动）：
  1. 从 icebreaker_scripts 取 enabled=1、marketing_feel <= 2 的候选池
  2. 排除最近 30 天已发过的 script_id（status='sent' 的 impression）
  3. freq_tier 权重：common=3 / mid_low=2 / low_freq=1
  4. 如果上一条 impression 同类型，对同类话术权重 -2（不连续原则）
  5. 在前 5 名候选中随机扰动取 1 条（避免永远选第 1 名）

如果候选池为空，返回 None，不发送、不报错。
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.db import (
    create_icebreaker_impression,
    get_last_icebreaker_impression,
    list_accounts_due_for_icebreaker,
    list_icebreaker_scripts,
    list_recent_icebreaker_script_ids,
)
from app.proactive._common import _select_route
from app.proactive.messaging import dispatch_proactive_text
from app.time_utils import beijing_naive_now


logger = logging.getLogger("ai4all.proactive.icebreaker")

# freq_tier -> 基础权重
_FREQ_WEIGHT: Dict[str, int] = {
    "common": 3,
    "mid_low": 2,
    "low_freq": 1,
}

# 同类型惩罚（上一条 impression 与候选同类型时扣分）
_SAME_TYPE_PENALTY = 2

# 候选池 top-N 中随机扰动取 1 条（N 为池大小的上限，避免尾端垃圾）
_TOP_K_RANDOM = 5

# 30 天去重窗口（秒）
_DEDUP_DAYS = 30


def pick_icebreaker_script(
    account_id: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """为账号选取一条合适的破冰话术，返回 icebreaker_scripts 行字典；无候选时返回 None。"""
    current = now or beijing_naive_now()
    since_dt = current - timedelta(days=_DEDUP_DAYS)
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 最近 30 天已发送过的 script_id（status='sent'，cancelled 不算）
    recent_ids = list_recent_icebreaker_script_ids(
        account_id=account_id,
        since=since_str,
    )

    # 候选池：enabled=1，marketing_feel <= 2，排除最近已发
    candidates = list_icebreaker_scripts(
        enabled_only=True,
        max_marketing_feel=2,
        exclude_ids=recent_ids if recent_ids else None,
    )

    if not candidates:
        logger.debug("pick_icebreaker_script: no candidates for account=%s", account_id)
        return None

    # 上一条 impression 用于类型轮换判断
    last = get_last_icebreaker_impression(account_id=account_id)
    last_type: Optional[str] = last["script_type"] if last else None

    # 为每个候选计算权重
    def _weight(script: Dict[str, Any]) -> int:
        w = _FREQ_WEIGHT.get(script.get("freq_tier", ""), 1)
        if last_type and script.get("script_type") == last_type:
            w -= _SAME_TYPE_PENALTY
        return w

    scored = sorted(candidates, key=_weight, reverse=True)

    # top-K 内随机扰动（权重相同时打破固定顺序）
    top_k = scored[: min(_TOP_K_RANDOM, len(scored))]
    return random.choice(top_k)


def dispatch_icebreaker(
    account_id: str,
    *,
    now: Optional[datetime] = None,
    trigger_source: str = "scheduler",
) -> Dict[str, Any]:
    """为单个账号派发一条破冰话术，返回操作结果字典。

    结果字段：
      account_id, status, reason, script_id (若选到), impression_id (若写入)
    """
    current = now or beijing_naive_now()

    route = _select_route(account_id)
    if route is None:
        return {"account_id": account_id, "status": "no_op", "reason": "missing_channel_route"}

    script = pick_icebreaker_script(account_id, now=current)
    if script is None:
        return {"account_id": account_id, "status": "no_op", "reason": "no_candidate_scripts"}

    script_id = script["id"]
    idempotency_key = f"icebreaker-{account_id}-{script_id}-{current.date().isoformat()}-{uuid.uuid4().hex[:8]}"

    # Proactive Selection Trace MVP：记录"这条话术为什么被选中"
    last_imp = get_last_icebreaker_impression(account_id=account_id)
    selection_trace = {
        "decision_trace": {
            "trace_type": "proactive_selection_trace",
            "trace_version": 1,
            "l0_context": {
                "last_icebreaker_at": last_imp["created_at"] if last_imp else None,
                "last_category": last_imp["script_type"] if last_imp else None,
            },
            "l1_trigger": {
                "trigger_type": "icebreaker",
                "trigger_source": trigger_source,
            },
            "l3_how": {
                "script_id": script["id"],
                "script_type": script.get("script_type"),
                "marketing_feel": script.get("marketing_feel"),
                "reply_cost": script.get("reply_cost"),
                "freq_tier": script.get("freq_tier"),
            },
        }
    }

    try:
        outbound = dispatch_proactive_text(
            account_id=account_id,
            channel=route["channel"],
            channel_account_id=route["channel_account_id"],
            to_user_id=route["to_user_id"],
            session_key=route.get("session_key"),
            source="icebreaker",
            text=script["text"],
            idempotency_key=idempotency_key,
            now=current,
            metadata=selection_trace,
        )
    except Exception as exc:
        logger.exception("dispatch_proactive_text failed for account=%s script=%s", account_id, script_id)
        return {
            "account_id": account_id,
            "status": "error",
            "reason": str(exc),
            "script_id": script_id,
        }

    outbound_status = outbound.get("status", "")
    # policy 放行 -> sent/pending/sending；policy 拦截 -> cancelled
    impression_status = "cancelled" if outbound_status == "cancelled" else "sent"

    try:
        impression_id = create_icebreaker_impression(
            account_id=account_id,
            script_id=script_id,
            outbound_message_id=outbound.get("id"),
            script_type=script.get("script_type", ""),
            marketing_feel=script.get("marketing_feel"),
            status=impression_status,
        )
    except Exception as exc:
        logger.exception("create_icebreaker_impression failed for account=%s", account_id)
        impression_id = None

    return {
        "account_id": account_id,
        "status": impression_status,
        "script_id": script_id,
        "outbound_id": outbound.get("id"),
        "outbound_status": outbound_status,
        "impression_id": impression_id,
    }


def dispatch_due_icebreakers(
    *,
    now: Optional[datetime] = None,
    limit: int = 50,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """扫描今日未收到破冰话术的账号并派发。供 ProactiveScheduler Step 5 调用。"""
    current = now or beijing_naive_now()
    quota_date = current.date().isoformat()

    due_accounts = list_accounts_due_for_icebreaker(
        quota_date=quota_date,
        limit=limit,
        node_id=node_id,
    )

    results: List[Dict[str, Any]] = []
    for account_id in due_accounts:
        result = dispatch_icebreaker(account_id, now=current)
        results.append(result)
        logger.debug(
            "icebreaker dispatch: account=%s status=%s script=%s",
            account_id,
            result.get("status"),
            result.get("script_id"),
        )

    return results
