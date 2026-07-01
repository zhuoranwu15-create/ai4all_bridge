"""近期热点（hot_topic）召回 —— 首个真·全局召回。

两个职责、两种 scope：
- `refresh_hot_topic_pool`（scope=global，每日一次）：搜索近 24h 热点 → LLM 抽主题 → 历史去重 →
  入全局候选池（无主，所有账号共享）。**不针对任何账号**，故不走 content_invitation 那条强依赖
  TurnContext 的工具循环，改为直接 `run_headless_web_search` + `generate_completion`。
- `select_hot_topic_candidate`（scope=account，planning 时每账号一次）：读全局池 → 用该账号的
  长期记忆 + 近 N 条聊天做 LLM 相关性打分 → 取 top_k → 多样性打散（近 3 次已推降权）→ top1 →
  组装成 `type=hot_topic` 的拉活候选（形状对齐 topic_followup 的 `*_candidate_created` 返回）。

账号隔离：全局池是无主数据（见 store.global_candidates 说明）；LLM 打分只喂**该账号自己**的
记忆/聊天，选中的 top1 只写进该账号自己的 reactivation 候选，无跨账号泄漏。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.time_utils import beijing_naive_now
from app.db import (
    get_account,
    get_proactive_account_state,
    list_recent_messages_for_account_since,
    list_recent_reactivation_outbound_messages,
)
from app.llm import generate_completion, is_llm_configured
from app.user_profiles import read_agent_context
from app.tools.web_search_handlers import run_headless_web_search
from app.proactive.contract.common import _clean_text, _extract_json_object, _select_route, _truncate_text
from app.proactive.contract.prompts import (
    HOT_TOPIC_PERSONALIZE_SYSTEM_PROMPT,
    HOT_TOPIC_RANK_SYSTEM_PROMPT,
    HOT_TOPIC_RECALL_SYSTEM_PROMPT,
)
from app.proactive.recall._shared import _format_decision_time, _no_op
from app.proactive.selection.diversity import rank_and_diversify
from app.proactive.store.candidates import REACTIVATION_TYPE_HOT_TOPIC, _normalize_dedupe_key
from app.proactive.store.global_candidates import (
    active_global_pool,
    add_global_candidate,
    recent_global_dedupe_keys,
)


HOT_TOPIC_KIND = REACTIVATION_TYPE_HOT_TOPIC

# hot_topic 打分时聊天回看窗口：热点主要面向不活跃/待拉活用户，72h 常为空，故放宽到 30 天，
# 让 LLM 至少能结合较久的兴趣线索（配合 memory）。仍受 context_messages 条数上限约束。
_PROFILE_WINDOW_DAYS = 30


def refresh_hot_topic_pool(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """全局召回：搜索近 24h 热点 → 抽主题 → 历史去重 → 入池。幂等（当日池已在即跳过）。"""
    current = now or beijing_naive_now()

    if not bool(getattr(settings, "hot_topic_recall_enabled", False)):
        return _no_op(account_id="", reason="hot_topic_recall_disabled", now=current)
    if not is_llm_configured():
        return _no_op(account_id="", reason="llm_disabled", now=current)
    if not bool(getattr(settings, "web_search_enabled", False)):
        return _no_op(account_id="", reason="web_search_disabled", now=current)

    today = current.date().isoformat()
    pool_today = [
        item
        for item in active_global_pool(kind=HOT_TOPIC_KIND, now=current, limit=100)
        if item.get("generated_date") == today
    ]
    if pool_today:
        return _no_op(
            account_id="",
            reason="hot_topic_pool_already_generated_today",
            now=current,
            metadata={"existing_count": len(pool_today)},
        )

    query = _clean_text(getattr(settings, "hot_topic_recall_query", "")) or "过去24小时国内外热点新闻话题"
    try:
        count = int(getattr(settings, "web_search_max_results", 5) or 5)
    except (TypeError, ValueError):
        count = 5
    search = run_headless_web_search(query, count=count, args={"freshness": "day"})
    if search.get("status") != "succeeded":
        return _no_op(
            account_id="",
            reason="hot_topic_search_failed",
            now=current,
            metadata={"error": search.get("error")},
        )
    results = search.get("results") or []
    if not results:
        return _no_op(account_id="", reason="hot_topic_no_search_results", now=current)

    result_lines = [
        f"- {_clean_text(item.get('title'))}: {_truncate_text(_clean_text(item.get('snippet')), 200)}"
        for item in results
        if _clean_text(item.get("title")) or _clean_text(item.get("snippet"))
    ]
    try:
        pool_size = int(getattr(settings, "hot_topic_pool_size", 8) or 8)
    except (TypeError, ValueError):
        pool_size = 8
    messages = [
        {"role": "system", "content": HOT_TOPIC_RECALL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"now: {_format_decision_time(current)}",
                    f"max_themes: {pool_size}",
                    "search_results:\n" + ("\n".join(result_lines) if result_lines else "- none"),
                ]
            ),
        },
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        themes = payload.get("themes") if isinstance(payload, dict) else None
    except Exception as err:  # noqa: BLE001 — 抽取失败按空处理，不抛断整轮调度
        return _no_op(
            account_id="",
            reason="hot_topic_extraction_failed",
            now=current,
            metadata={"error": str(err)},
        )
    if not isinstance(themes, list) or not themes:
        return _no_op(account_id="", reason="hot_topic_no_themes", now=current)

    try:
        lookback_days = int(getattr(settings, "hot_topic_history_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        lookback_days = 3
    try:
        ttl_hours = int(getattr(settings, "hot_topic_ttl_hours", 24) or 24)
    except (TypeError, ValueError):
        ttl_hours = 24

    seen = recent_global_dedupe_keys(kind=HOT_TOPIC_KIND, now=current, lookback_days=lookback_days)
    inserted: List[Dict[str, Any]] = []
    for theme in themes[:pool_size]:
        if not isinstance(theme, dict):
            continue
        topic = _clean_text(theme.get("topic"))
        text = _clean_text(theme.get("text"))
        if not text:
            continue
        key = _normalize_dedupe_key(topic or text)
        if not key or key in seen:
            continue  # 历史去重：近 N 天已入池的同主题不再入
        seen.add(key)
        add_global_candidate(
            kind=HOT_TOPIC_KIND,
            topic=topic or None,
            text=text[:120],
            now=current,
            ttl_hours=ttl_hours,
            metadata={"source": "hot_topic_recall", "query": query},
        )
        inserted.append({"topic": topic, "text": text[:120]})

    return {
        "action": "hot_topic_pool_refreshed",
        "account_id": "",
        "inserted_count": len(inserted),
        "candidates": inserted,
        "evaluated_at": _format_decision_time(current),
    }


def _build_hot_topic_rank_prompt(
    *,
    account: Dict[str, Any],
    memory: str,
    history: List[Dict[str, Any]],
    pool: List[Dict[str, Any]],
    now: datetime,
) -> str:
    history_lines = [
        f"- {item['role']}: {_truncate_text(_clean_text(item.get('content')), 200)}"
        for item in history
        if _clean_text(item.get("content"))
    ]
    pool_lines = [
        f"- id={item['id']} topic={_clean_text(item.get('topic'))} text={_truncate_text(_clean_text(item.get('text')), 80)}"
        for item in pool
    ]
    return "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account['id']}",
            "MEMORY.md:\n" + _truncate_text(memory, 1600),
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
            "hot_topic_pool:\n" + ("\n".join(pool_lines) if pool_lines else "- none"),
        ]
    )


def _personalize_hot_topic_text(
    *,
    account: Dict[str, Any],
    soul: str,
    memory: str,
    history: List[Dict[str, Any]],
    topic: str,
    base_text: str,
    now: datetime,
) -> str:
    """把池内通用 hook 按该账号的人格+记忆改写成个性化微信消息。失败/空则回退原文。"""
    history_lines = [
        f"- {item['role']}: {_truncate_text(_clean_text(item.get('content')), 200)}"
        for item in history
        if _clean_text(item.get("content"))
    ]
    user_prompt = "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account['id']}",
            "SOUL.md:\n" + _truncate_text(soul, 1000),
            "MEMORY.md:\n" + _truncate_text(memory, 1600),
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
            f"hot_topic: {topic}",
            f"base_text: {base_text}",
        ]
    )
    messages = [
        {"role": "system", "content": HOT_TOPIC_PERSONALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        text = _clean_text(payload.get("text")) if isinstance(payload, dict) else ""
    except Exception:  # noqa: BLE001 — 个性化失败不阻断发送，回退通用 hook
        return base_text
    return text or base_text


def select_hot_topic_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """每账号：读全局池 → LLM 相关性打分 → 多样性打散 → top1 拉活候选。"""
    current = now or beijing_naive_now()

    account = get_account(account_id=account_id)
    if account is None:
        return _no_op(account_id=account_id, reason="account_not_found", now=current)
    if account.get("status") != "active":
        return _no_op(account_id=account_id, reason="account_not_active", now=current)

    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return _no_op(account_id=account_id, reason="proactive_state_missing", now=current)
    if not state.get("enabled"):
        return _no_op(account_id=account_id, reason="proactive_disabled", now=current)

    if not is_llm_configured():
        return _no_op(account_id=account_id, reason="llm_disabled", now=current)
    if _select_route(account_id) is None:
        return _no_op(account_id=account_id, reason="missing_channel_route", now=current)

    try:
        pool_size = int(getattr(settings, "hot_topic_pool_size", 8) or 8)
    except (TypeError, ValueError):
        pool_size = 8
    pool = active_global_pool(kind=HOT_TOPIC_KIND, now=current, limit=max(pool_size * 2, 16))
    if not pool:
        return _no_op(account_id=account_id, reason="no_hot_topic_pool", now=current)

    agent_context = read_agent_context(account_id, display_name=account.get("display_name"))
    memory = agent_context.blocks.get("MEMORY", "")
    try:
        context_messages = int(getattr(settings, "hot_topic_profile_context_messages", 50) or 50)
    except (TypeError, ValueError):
        context_messages = 50
    since_local = _format_decision_time(current - timedelta(days=_PROFILE_WINDOW_DAYS))
    history = list_recent_messages_for_account_since(
        account_id=account_id,
        since=since_local,
        limit=max(1, context_messages),
    )
    # memory 与 history 均空时 LLM 无从判断，直接跳过，避免无依据硬发。
    if not _clean_text(memory) and not history:
        return _no_op(account_id=account_id, reason="no_hot_topic_profile_signal", now=current)

    messages = [
        {"role": "system", "content": HOT_TOPIC_RANK_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_hot_topic_rank_prompt(
                account=account,
                memory=memory,
                history=history,
                pool=pool,
                now=current,
            ),
        },
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        ranked = payload.get("ranked") if isinstance(payload, dict) else None
    except Exception as err:  # noqa: BLE001
        return _no_op(
            account_id=account_id,
            reason="hot_topic_ranking_failed",
            now=current,
            metadata={"error": str(err)},
        )
    if not isinstance(ranked, list) or not ranked:
        return _no_op(account_id=account_id, reason="llm_no_hot_topic_selection", now=current)

    try:
        min_score = float(getattr(settings, "hot_topic_min_score", 0.3) or 0.0)
    except (TypeError, ValueError):
        min_score = 0.3
    by_id = {int(item["id"]): item for item in pool}
    scored: List[Dict[str, Any]] = []
    for entry in ranked:
        if not isinstance(entry, dict):
            continue
        try:
            cid = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        pool_item = by_id.get(cid)
        if pool_item is None:
            continue
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            continue
        scored.append(
            {
                "id": cid,
                "topic": _clean_text(pool_item.get("topic")),
                "text": _clean_text(pool_item.get("text")),
                "score": max(0.0, min(score, 1.0)),
                "reason": _clean_text(entry.get("reason")),
            }
        )
    if not scored:
        return _no_op(account_id=account_id, reason="hot_topic_below_min_score", now=current)

    # 多样性：近 dedupe_days 天已推拉活的 topic（取最近几条）作为降权目标。
    try:
        dedupe_days = int(getattr(settings, "reactivation_dedupe_days", 3) or 3)
    except (TypeError, ValueError):
        dedupe_days = 3
    sent_history = list_recent_reactivation_outbound_messages(
        account_id=account_id,
        since=_format_decision_time(current - timedelta(days=max(dedupe_days, 1))),
        limit=3,
    )
    recent_topics = [
        (item.get("metadata") or {}).get("topic")
        for item in sent_history
        if (item.get("metadata") or {}).get("topic")
    ]
    try:
        top_k = int(getattr(settings, "hot_topic_select_top_k", 3) or 3)
    except (TypeError, ValueError):
        top_k = 3
    diversified = rank_and_diversify(scored, recent_topics, top_k=max(top_k, 1))
    if not diversified:
        return _no_op(account_id=account_id, reason="hot_topic_no_candidate_after_diversify", now=current)

    winner = diversified[0]
    base_text = winner["text"]
    if not base_text:
        return _no_op(account_id=account_id, reason="hot_topic_winner_text_empty", now=current)
    # 个性化改写：把池内通用 hook 结合该账号人格(SOUL)+记忆+近聊重写成发给这个用户的口语消息。
    # 失败/空回退通用 hook（不阻断发送）。仅对选中的 top1 做一次，不对整池。
    personalized = _personalize_hot_topic_text(
        account=account,
        soul=agent_context.blocks.get("SOUL", ""),
        memory=memory,
        history=history,
        topic=winner["topic"],
        base_text=base_text,
        now=current,
    )
    candidate: Dict[str, Any] = {
        "id": "reactivation-hottopic-" + current.strftime("%Y%m%d%H%M%S") + f"{current.microsecond:06d}",
        "type": REACTIVATION_TYPE_HOT_TOPIC,
        "text": personalized[:120],
        "topic": winner["topic"] or "hot_topic",
        "reason": winner.get("reason") or "hot_topic_llm_selected",
        "confidence": winner["score"],
        "generated_at": _format_decision_time(current),
        "metadata": {
            "source": "hot_topic",
            "global_candidate_id": winner["id"],
            "rank_score": winner["score"],
            "base_text": base_text,
            "personalized": personalized != base_text,
        },
    }
    return {
        "action": "hot_topic_candidate_created",
        "account_id": account_id,
        "reactivation_candidate": candidate,
        "evaluated_at": _format_decision_time(current),
    }
