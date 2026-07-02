"""近期热点（hot_topic）召回 —— 首个真·全局召回。

两个职责、两种 scope：
- `refresh_hot_topic_pool`（scope=global，每日按绝对时钟点档位刷新，默认 10:00/17:00）：搜索
  近 24h 热点 → LLM 抽主题 → 历史去重 → 入全局候选池（无主，所有账号共享）。**不针对任何账号**，
  故不走 content_invitation 那条强依赖 TurnContext 的工具循环，改为直接 `run_headless_web_search`
  + `generate_completion`。
- `select_hot_topic_candidate`（scope=account，planning 时每账号一次，只在 topic_followup/
  content_invitation 均空时兜底触发）：优先读该账号的 `hot_topic_reserve` 储备缓存（3 条已改写
  候选，`hot_topic_account_reserve_ttl_hours` 有效期），命中未用条目直接按 rank_score 取最高一条、
  标记已用、0 次 LLM 调用；缓存缺失/全过期/全部用完才重新生成：读全局池 → 用该账号的长期记忆 +
  近 N 条聊天做 LLM 相关性打分（1 次调用）→ top_k 多样性打散（近期已推降权）→ 批量个性化改写
  全部 top_k 条（1 次调用）→ 写回储备缓存，取其中最高分一条提交为本次拉活候选并标记已用。
  结果形状对齐 topic_followup 的 `*_candidate_created` 返回。

账号隔离：全局池是无主数据（见 store.global_candidates 说明）；LLM 打分/改写只喂**该账号自己**的
记忆/聊天，储备缓存与最终候选都只写进该账号自己的 proactive_account_state，无跨账号泄漏。
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
    upsert_proactive_account_state,
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
from app.proactive.recall._hot_list import collect_hot_list_lines
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

# 账号级 hot_topic 候选储备缓存，挂在 proactive_account_state.metadata_json 的独立 key，
# 与单一发送槽位 reactivation_candidate 互不冲突（见 store/candidates.py）。
HOT_TOPIC_RESERVE_METADATA_KEY = "hot_topic_reserve"


def _parse_pool_refresh_slots() -> List[str]:
    raw = getattr(settings, "hot_topic_pool_refresh_slots", "10:00,17:00")
    slots: List[str] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        try:
            datetime.strptime(text, "%H:%M")
        except ValueError:
            continue
        slots.append(text)
    return slots or ["10:00", "17:00"]


def _current_pool_refresh_slot_key(now: datetime) -> Optional[str]:
    """返回 now 已跨过的最新档位 key（date_slotN）；今日所有档位都未到则 None。

    只按"是否已过档口时间点"判断，不要求精确命中，天然支持补跑——服务在两个档口之间
    重启后，第一次 tick 就能发现"今日该档未生成过"并补上。
    """
    slots = _parse_pool_refresh_slots()
    current_index: Optional[int] = None
    for index, slot in enumerate(slots):
        hour, minute = (int(part) for part in slot.split(":", 1))
        slot_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if slot_dt <= now:
            current_index = index
    if current_index is None:
        return None
    return f"{now.date().isoformat()}_slot{current_index}"


def refresh_hot_topic_pool(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """全局召回：搜索近 24h 热点 → 抽主题 → 历史去重 → 入池。

    绝对时钟点触发（`hot_topic_pool_refresh_slots`，默认 10:00/17:00），幂等门控按
    "当日+档位" 而非"当日"——允许补跑，只要求该档今日未生成过。
    """
    current = now or beijing_naive_now()

    if not bool(getattr(settings, "hot_topic_recall_enabled", False)):
        return _no_op(account_id="", reason="hot_topic_recall_disabled", now=current)

    slot_key = _current_pool_refresh_slot_key(current)
    if slot_key is None:
        return _no_op(account_id="", reason="hot_topic_pool_refresh_not_due", now=current)

    if not is_llm_configured():
        return _no_op(account_id="", reason="llm_disabled", now=current)

    # 数据来源门控：热榜或 web search 至少配置一个
    hot_topic_sources_raw = _clean_text(getattr(settings, "hot_topic_sources", "")) or ""
    configured_sources = [s.strip() for s in hot_topic_sources_raw.split(",") if s.strip()]
    web_search_ok = bool(getattr(settings, "web_search_enabled", False))
    if not configured_sources and not web_search_ok:
        return _no_op(account_id="", reason="hot_topic_no_data_source", now=current)

    today = current.date().isoformat()
    pool_slot = [
        item
        for item in active_global_pool(kind=HOT_TOPIC_KIND, now=current, limit=100)
        if item.get("generated_date") == today and (item.get("metadata") or {}).get("slot_key") == slot_key
    ]
    if pool_slot:
        return _no_op(
            account_id="",
            reason="hot_topic_pool_already_generated_for_slot",
            now=current,
            metadata={"existing_count": len(pool_slot), "slot_key": slot_key},
        )

    # Step 1：优先从热榜抓取
    result_lines: list = []
    data_source = "none"
    if configured_sources:
        source_urls = {
            "toutiao": _clean_text(getattr(settings, "hot_topic_toutiao_url", "")) or "",
            "zhihu": _clean_text(getattr(settings, "hot_topic_zhihu_url", "")) or "",
        }
        source_backup_urls = {
            "zhihu": _clean_text(getattr(settings, "hot_topic_zhihu_backup_url", "")) or "",
        }
        try:
            fetch_timeout = float(getattr(settings, "hot_topic_fetch_timeout_seconds", 5.0) or 5.0)
            max_items = int(getattr(settings, "hot_topic_max_items_per_source", 20) or 20)
        except (TypeError, ValueError):
            fetch_timeout, max_items = 5.0, 20
        result_lines = collect_hot_list_lines(
            sources=configured_sources,
            source_urls=source_urls,
            source_backup_urls=source_backup_urls,
            timeout=fetch_timeout,
            max_items_per_source=max_items,
        )
        if result_lines:
            data_source = "hot_list"

    # Step 2：热榜全部失败时降级 web search
    if not result_lines and web_search_ok:
        query = _clean_text(getattr(settings, "hot_topic_recall_query", "")) or "过去24小时国内外热点新闻话题"
        try:
            count = int(getattr(settings, "web_search_max_results", 5) or 5)
        except (TypeError, ValueError):
            count = 5
        search = run_headless_web_search(query, count=count, args={"freshness": "day"})
        if search.get("status") == "succeeded":
            search_results = search.get("results") or []
            result_lines = [
                f"- {_clean_text(item.get('title'))}: {_truncate_text(_clean_text(item.get('snippet')), 200)}"
                for item in search_results
                if _clean_text(item.get("title")) or _clean_text(item.get("snippet"))
            ]
            if result_lines:
                data_source = "web_search"

    if not result_lines:
        return _no_op(account_id="", reason="hot_topic_no_data", now=current)
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
        lookback_days = int(getattr(settings, "hot_topic_history_dedupe_days", 1) or 1)
    except (TypeError, ValueError):
        lookback_days = 1
    try:
        ttl_hours = int(getattr(settings, "hot_topic_ttl_hours", 12) or 12)
    except (TypeError, ValueError):
        ttl_hours = 12

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
            metadata={"source": "hot_topic_recall", "data_source": data_source, "slot_key": slot_key},
        )
        inserted.append({"topic": topic, "text": text[:120]})

    return {
        "action": "hot_topic_pool_refreshed",
        "account_id": "",
        "data_source": data_source,
        "slot_key": slot_key,
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


def _personalize_hot_topic_batch(
    *,
    account: Dict[str, Any],
    soul: str,
    memory: str,
    history: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    now: datetime,
) -> Dict[int, str]:
    """一次 LLM 调用批量把池内通用 hook 改写成该账号的个性化微信消息。

    返回 {global_candidate_id: 改写后文案}；调用失败或某条缺失改写结果时，
    该 id 不出现在返回 dict 里，由调用方回退该条自己的 base_text。
    """
    history_lines = [
        f"- {item['role']}: {_truncate_text(_clean_text(item.get('content')), 200)}"
        for item in history
        if _clean_text(item.get("content"))
    ]
    item_lines = [
        f"- id={item['id']} topic={_clean_text(item.get('topic'))} base_text={_truncate_text(_clean_text(item.get('text')), 80)}"
        for item in items
    ]
    user_prompt = "\n\n".join(
        [
            f"now: {_format_decision_time(now)}",
            f"account_id: {account['id']}",
            "SOUL.md:\n" + _truncate_text(soul, 1000),
            "MEMORY.md:\n" + _truncate_text(memory, 1600),
            "recent_chat:\n" + ("\n".join(history_lines) if history_lines else "- none"),
            "items:\n" + ("\n".join(item_lines) if item_lines else "- none"),
        ]
    )
    messages = [
        {"role": "system", "content": HOT_TOPIC_PERSONALIZE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        raw = generate_completion(messages)
        payload = _extract_json_object(raw)
        rewritten = payload.get("items") if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 — 批量个性化失败不阻断发送，调用方按条回退 base_text
        return {}
    result: Dict[int, str] = {}
    if not isinstance(rewritten, list):
        return result
    for entry in rewritten:
        if not isinstance(entry, dict):
            continue
        try:
            entry_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        text = _clean_text(entry.get("text"))
        if text:
            result[entry_id] = text
    return result


def _build_hot_topic_candidate(item: Dict[str, Any], *, current: datetime, cache_hit: bool) -> Dict[str, Any]:
    """把储备缓存条目（无论新生成还是缓存命中）组装成 `type=hot_topic` 拉活候选。"""
    text = _clean_text(item.get("text")) or _clean_text(item.get("base_text"))
    base_text = _clean_text(item.get("base_text")) or text
    return {
        "id": "reactivation-hottopic-" + current.strftime("%Y%m%d%H%M%S") + f"{current.microsecond:06d}",
        "type": REACTIVATION_TYPE_HOT_TOPIC,
        "text": text[:120],
        "topic": item.get("topic") or "hot_topic",
        "reason": item.get("reason") or "hot_topic_llm_selected",
        "confidence": item.get("score"),
        "generated_at": _format_decision_time(current),
        "metadata": {
            "source": "hot_topic",
            "global_candidate_id": item.get("id"),
            "rank_score": item.get("score"),
            "base_text": base_text,
            "personalized": text != base_text,
            "cache_hit": cache_hit,
        },
    }


def _pick_reserve_candidate(
    reserve: Optional[Dict[str, Any]],
    *,
    now_str: str,
) -> Optional[Dict[str, Any]]:
    """从账号级储备缓存里挑一条未过期、未用、rank_score 最高的候选；无则 None。"""
    if not isinstance(reserve, dict):
        return None
    if _clean_text(reserve.get("expires_at")) <= now_str:
        return None
    items = reserve.get("candidates")
    if not isinstance(items, list):
        return None
    unused = [item for item in items if isinstance(item, dict) and not item.get("used")]
    if not unused:
        return None
    return max(unused, key=lambda item: float(item.get("score") or 0.0))


def _mark_reserve_candidate_used(reserve: Dict[str, Any], *, candidate_id: Any) -> Dict[str, Any]:
    next_reserve = dict(reserve)
    next_candidates = []
    for item in reserve.get("candidates") or []:
        if isinstance(item, dict) and item.get("id") == candidate_id:
            item = dict(item)
            item["used"] = True
        next_candidates.append(item)
    next_reserve["candidates"] = next_candidates
    return next_reserve


def select_hot_topic_candidate(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """每账号：储备缓存命中直接取（0 次 LLM）；未命中才读全局池 → 打分 → 打散 → 批量个性化。"""
    current = now or beijing_naive_now()

    # 与 refresh_hot_topic_pool 共用总开关：关闭时账号级选择也整体跳过（不读池、不调 LLM），
    # 而不仅仅停止池刷新——避免关闭 recall 后 account_checks 仍消耗未过期(ttl 24h)的旧池数据。
    if not bool(getattr(settings, "hot_topic_recall_enabled", False)):
        return _no_op(account_id=account_id, reason="hot_topic_recall_disabled", now=current)

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

    now_str = _format_decision_time(current)
    reserve = (state.get("metadata") or {}).get(HOT_TOPIC_RESERVE_METADATA_KEY)
    picked = _pick_reserve_candidate(reserve, now_str=now_str)
    if picked is not None:
        updated_reserve = _mark_reserve_candidate_used(reserve, candidate_id=picked.get("id"))
        upsert_proactive_account_state(
            account_id=account_id,
            metadata_patch={HOT_TOPIC_RESERVE_METADATA_KEY: updated_reserve},
        )
        candidate = _build_hot_topic_candidate(picked, current=current, cache_hit=True)
        return {
            "action": "hot_topic_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": candidate,
            "evaluated_at": now_str,
        }

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

    diversified = [item for item in diversified if item.get("text")]
    if not diversified:
        return _no_op(account_id=account_id, reason="hot_topic_winner_text_empty", now=current)

    # 批量个性化改写：把池内通用 hook 结合该账号人格(SOUL)+记忆+近聊，一次 LLM 调用重写
    # 全部 top_k 条（而非只重写 top1），失败/缺项时逐条回退各自的通用 hook（不阻断发送）。
    personalized_map = _personalize_hot_topic_batch(
        account=account,
        soul=agent_context.blocks.get("SOUL", ""),
        memory=memory,
        history=history,
        items=diversified,
        now=current,
    )
    try:
        reserve_ttl_hours = int(getattr(settings, "hot_topic_account_reserve_ttl_hours", 6) or 6)
    except (TypeError, ValueError):
        reserve_ttl_hours = 6
    reserve_candidates: List[Dict[str, Any]] = []
    for item in diversified:
        base_text = item["text"]
        personalized_text = personalized_map.get(item["id"]) or base_text
        reserve_candidates.append(
            {
                "id": item["id"],
                "topic": item["topic"],
                "text": personalized_text[:120],
                "base_text": base_text,
                "score": item["score"],
                "reason": item.get("reason") or "hot_topic_llm_selected",
                "used": False,
            }
        )

    winner = max(reserve_candidates, key=lambda item: item["score"])
    new_reserve = {
        "generated_at": now_str,
        "expires_at": _format_decision_time(current + timedelta(hours=max(reserve_ttl_hours, 1))),
        "candidates": reserve_candidates,
    }
    committed_reserve = _mark_reserve_candidate_used(new_reserve, candidate_id=winner["id"])
    upsert_proactive_account_state(
        account_id=account_id,
        metadata_patch={HOT_TOPIC_RESERVE_METADATA_KEY: committed_reserve},
    )
    candidate = _build_hot_topic_candidate(winner, current=current, cache_hit=False)
    return {
        "action": "hot_topic_candidate_created",
        "account_id": account_id,
        "reactivation_candidate": candidate,
        "evaluated_at": now_str,
    }
