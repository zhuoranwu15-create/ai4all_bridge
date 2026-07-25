"""全局（无主）候选池的存储封装 —— 账号隔离不变量的**显式例外**。

见 `_migration_0011`：`proactive_global_candidates` 刻意无 `account_id`，存"所有账号只读
共享的无主候选池"（近期热点等全局召回产物）。本模块是该表的薄封装：入池（带历史去重）+
读活跃池 + 取历史 dedupe_key。**本层只碰全局池，绝不写任何账号维度数据**；池→账号的绑定
由选择层在选中 top1 时完成（盖 account_id 写进该账号自己的 reactivation 候选）。

dedupe_key 复用 `store.candidates._normalize_dedupe_key` 的口径（小写+折叠空白），与账号级
精确去重保持一致，避免两处归一化漂移。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from app.db import (
    insert_global_candidate,
    list_active_global_candidates,
    list_recent_global_candidate_keys,
)
from app.products.zhaoxi.proactive.contract.common import format_reactivation_time
from app.products.zhaoxi.proactive.store.candidates import _normalize_dedupe_key


def recent_global_dedupe_keys(
    *,
    kind: str,
    now: datetime,
    lookback_days: int,
) -> Set[str]:
    """近 `lookback_days` 天该 kind 已入池的 dedupe_key 集合（历史去重依据）。"""
    since_date = (now - timedelta(days=max(lookback_days, 1))).date().isoformat()
    return set(list_recent_global_candidate_keys(kind=kind, since_date=since_date))


def add_global_candidate(
    *,
    kind: str,
    topic: Optional[str],
    text: str,
    now: datetime,
    ttl_hours: int,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """入池一条全局候选，返回其归一化 dedupe_key。

    dedupe_key 取 topic（无则 text）归一化后的串；命中当日/历史 UNIQUE 时 DB 层静默忽略，幂等。
    """
    dedupe_key = _normalize_dedupe_key(topic or text)
    expires_at = format_reactivation_time(now + timedelta(hours=max(ttl_hours, 1)))
    insert_global_candidate(
        kind=kind,
        topic=(topic or "").strip() or None,
        text=text.strip(),
        generated_date=now.date().isoformat(),
        dedupe_key=dedupe_key,
        expires_at=expires_at,
        created_at=format_reactivation_time(now),
        metadata=metadata or {},
    )
    return dedupe_key


def active_global_pool(
    *,
    kind: str,
    now: datetime,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """未过期的某类全局候选池（按 now 过滤 TTL）。"""
    return list_active_global_candidates(
        kind=kind,
        now=format_reactivation_time(now),
        limit=limit,
    )
