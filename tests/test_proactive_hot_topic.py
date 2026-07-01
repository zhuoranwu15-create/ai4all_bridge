"""近期热点（hot_topic）召回：全局池 + 每账号选择 + 多样性 + planning 接入。"""
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

HT = "app.proactive.recall.hot_topic"


# --------------------------------------------------------------------------- helpers
def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


def _create_route(account_id: str) -> None:
    from app.db import upsert_channel_binding

    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key=f"session-{account_id}",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="user@im.wechat",
        raw_identity={"source": "test"},
    )


def _seed_pool(*, topic: str, text: str, generated_date: str, now: datetime, ttl_hours: int = 24) -> None:
    from app.proactive.store.global_candidates import add_global_candidate

    # 直接落一条全局候选（generated_date 由入参控制，用于区分"今天/历史"）。
    from app.db import insert_global_candidate
    from app.proactive.store.candidates import _normalize_dedupe_key
    from app.proactive.contract.common import format_reactivation_time
    from datetime import timedelta

    insert_global_candidate(
        kind="hot_topic",
        topic=topic,
        text=text,
        generated_date=generated_date,
        dedupe_key=_normalize_dedupe_key(topic),
        expires_at=format_reactivation_time(now + timedelta(hours=ttl_hours)),
        created_at=format_reactivation_time(now),
        metadata={"source": "seed"},
    )


def _settings(**overrides):
    base = dict(
        hot_topic_recall_enabled=True,
        web_search_enabled=True,
        hot_topic_recall_query="过去24小时热点",
        web_search_max_results=5,
        hot_topic_pool_size=8,
        hot_topic_history_dedupe_days=3,
        hot_topic_ttl_hours=24,
        hot_topic_min_score=0.3,
        hot_topic_select_top_k=3,
        hot_topic_profile_context_messages=50,
        reactivation_dedupe_days=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- diversity (纯函数)
def test_rank_and_diversify_trims_topk_and_downweights_recent():
    from app.proactive.selection.diversity import rank_and_diversify

    items = [
        {"id": 1, "topic": "露营", "score": 0.9},
        {"id": 2, "topic": "考研", "score": 0.8},
        {"id": 3, "topic": "美食", "score": 0.7},
        {"id": 4, "topic": "股票", "score": 0.6},  # 超出 top_k=3，被裁掉
    ]
    out = rank_and_diversify(items, recent_topics=["露营"], top_k=3, penalty=0.5)
    assert [it["id"] for it in out] == [2, 3, 1]  # 露营(0.9)近期已推→降权到 0.45，后置
    assert len(out) == 3
    assert out[0]["id"] == 2  # top1 变为考研


def test_rank_and_diversify_no_recent_keeps_relevance_order():
    from app.proactive.selection.diversity import rank_and_diversify

    items = [
        {"id": 1, "topic": "a", "score": 0.5},
        {"id": 2, "topic": "b", "score": 0.9},
    ]
    out = rank_and_diversify(items, recent_topics=[], top_k=3)
    assert [it["id"] for it in out] == [2, 1]


# --------------------------------------------------------------------------- 全局召回
def test_refresh_hot_topic_pool_disabled_noop(fresh_db):
    from app.proactive.recall.hot_topic import refresh_hot_topic_pool

    with patch(f"{HT}.settings", _settings(hot_topic_recall_enabled=False)):
        result = refresh_hot_topic_pool(now=datetime(2026, 7, 1, 9, 0))
    assert result["action"] == "no_op"
    assert result["reason"] == "hot_topic_recall_disabled"


def test_refresh_hot_topic_pool_inserts_with_history_dedup_and_idempotent(fresh_db):
    from app.proactive.recall.hot_topic import refresh_hot_topic_pool
    from app.proactive.store.global_candidates import active_global_pool

    now = datetime(2026, 7, 1, 9, 0)
    # 历史池：昨天已入过“露营”，本次应被历史去重滤除。
    _seed_pool(topic="露营", text="旧的露营", generated_date="2026-06-30", now=now)

    search_ret = {
        "status": "succeeded",
        "results": [
            {"title": "考研新政", "snippet": "考研相关"},
            {"title": "露营热", "snippet": "露营相关"},
        ],
    }
    themes_json = json.dumps(
        {"themes": [
            {"topic": "露营", "text": "最近想去露营吗"},   # 历史重复 → 跳过
            {"topic": "考研", "text": "考研准备咋样"},       # 新 → 入池
        ]},
        ensure_ascii=False,
    )
    with patch(f"{HT}.settings", _settings()), \
         patch(f"{HT}.is_llm_configured", return_value=True), \
         patch(f"{HT}.run_headless_web_search", return_value=search_ret), \
         patch(f"{HT}.generate_completion", return_value=themes_json):
        result = refresh_hot_topic_pool(now=now)
        assert result["action"] == "hot_topic_pool_refreshed"
        assert result["inserted_count"] == 1
        assert result["candidates"][0]["topic"] == "考研"

        # 幂等：当日池已生成 → 再跑 no_op，不重复调用搜索。
        result2 = refresh_hot_topic_pool(now=now)
        assert result2["action"] == "no_op"
        assert result2["reason"] == "hot_topic_pool_already_generated_today"

    today_pool = [c for c in active_global_pool(kind="hot_topic", now=now) if c["generated_date"] == "2026-07-01"]
    assert [c["topic"] for c in today_pool] == ["考研"]


def test_refresh_hot_topic_pool_search_failed_noop(fresh_db):
    from app.proactive.recall.hot_topic import refresh_hot_topic_pool

    with patch(f"{HT}.settings", _settings()), \
         patch(f"{HT}.is_llm_configured", return_value=True), \
         patch(f"{HT}.run_headless_web_search", return_value={"status": "failed", "error": "boom"}):
        result = refresh_hot_topic_pool(now=datetime(2026, 7, 1, 9, 0))
    assert result["action"] == "no_op"
    assert result["reason"] == "hot_topic_no_data"


# --------------------------------------------------------------------------- 每账号选择
def _memory_ctx(text: str):
    return SimpleNamespace(blocks={"MEMORY": text, "SOUL": "", "USER": ""})


def test_select_hot_topic_empty_pool_noop(fresh_db):
    from app.proactive.recall.hot_topic import select_hot_topic_candidate

    _create_account("acc-ht-empty")
    _create_route("acc-ht-empty")
    from app.proactive.store.account_state import ensure_account_state
    ensure_account_state(account_id="acc-ht-empty")

    with patch(f"{HT}.settings", _settings()), \
         patch(f"{HT}.is_llm_configured", return_value=True):
        result = select_hot_topic_candidate(account_id="acc-ht-empty", now=datetime(2026, 7, 1, 12, 0))
    assert result["action"] == "no_op"
    assert result["reason"] == "no_hot_topic_pool"


def test_select_hot_topic_picks_top1(fresh_db):
    from app.proactive.recall.hot_topic import select_hot_topic_candidate
    from app.proactive.store.global_candidates import active_global_pool
    from app.proactive.store.account_state import ensure_account_state

    now = datetime(2026, 7, 1, 12, 0)
    _create_account("acc-ht-pick")
    _create_route("acc-ht-pick")
    ensure_account_state(account_id="acc-ht-pick")
    _seed_pool(topic="露营", text="想去露营吗", generated_date="2026-07-01", now=now)
    _seed_pool(topic="摄影", text="最近拍照了吗", generated_date="2026-07-01", now=now)

    pool = active_global_pool(kind="hot_topic", now=now)
    by_topic = {c["topic"]: c["id"] for c in pool}
    ranked = json.dumps({"ranked": [
        {"id": by_topic["摄影"], "score": 0.92, "reason": "用户爱摄影"},
        {"id": by_topic["露营"], "score": 0.4, "reason": "一般"},
    ]})
    personalized = json.dumps({"text": "上次说的那组照片洗出来没？最近还拍吗"})

    with patch(f"{HT}.settings", _settings()), \
         patch(f"{HT}.is_llm_configured", return_value=True), \
         patch(f"{HT}.read_agent_context", return_value=_memory_ctx("用户喜欢摄影和旅行")), \
         patch(f"{HT}.generate_completion", side_effect=[ranked, personalized]):
        result = select_hot_topic_candidate(account_id="acc-ht-pick", now=now)

    assert result["action"] == "hot_topic_candidate_created"
    candidate = result["reactivation_candidate"]
    assert candidate["type"] == "hot_topic"
    # 最终发送文案是个性化改写后的，池内通用 hook 存于 metadata.base_text。
    assert candidate["text"] == "上次说的那组照片洗出来没？最近还拍吗"
    assert candidate["topic"] == "摄影"
    assert candidate["metadata"]["global_candidate_id"] == by_topic["摄影"]
    assert candidate["metadata"]["base_text"] == "最近拍照了吗"
    assert candidate["metadata"]["personalized"] is True
    assert candidate["confidence"] == 0.92


def test_select_hot_topic_personalize_failure_falls_back_to_hook(fresh_db):
    from app.proactive.recall.hot_topic import select_hot_topic_candidate
    from app.proactive.store.global_candidates import active_global_pool
    from app.proactive.store.account_state import ensure_account_state

    now = datetime(2026, 7, 1, 12, 0)
    _create_account("acc-ht-fb")
    _create_route("acc-ht-fb")
    ensure_account_state(account_id="acc-ht-fb")
    _seed_pool(topic="摄影", text="最近拍照了吗", generated_date="2026-07-01", now=now)

    pool = active_global_pool(kind="hot_topic", now=now)
    ranked = json.dumps({"ranked": [{"id": pool[0]["id"], "score": 0.9, "reason": "爱摄影"}]})

    # 第二次调用（个性化）返回非 JSON → 回退池内通用 hook，personalized=False。
    with patch(f"{HT}.settings", _settings()), \
         patch(f"{HT}.is_llm_configured", return_value=True), \
         patch(f"{HT}.read_agent_context", return_value=_memory_ctx("用户喜欢摄影")), \
         patch(f"{HT}.generate_completion", side_effect=[ranked, "对不起我不会"]):
        result = select_hot_topic_candidate(account_id="acc-ht-fb", now=now)

    candidate = result["reactivation_candidate"]
    assert candidate["text"] == "最近拍照了吗"
    assert candidate["metadata"]["personalized"] is False


def test_select_hot_topic_below_min_score_noop(fresh_db):
    from app.proactive.recall.hot_topic import select_hot_topic_candidate
    from app.proactive.store.global_candidates import active_global_pool
    from app.proactive.store.account_state import ensure_account_state

    now = datetime(2026, 7, 1, 12, 0)
    _create_account("acc-ht-low")
    _create_route("acc-ht-low")
    ensure_account_state(account_id="acc-ht-low")
    _seed_pool(topic="股票", text="看股票吗", generated_date="2026-07-01", now=now)

    pool = active_global_pool(kind="hot_topic", now=now)
    ranked = json.dumps({"ranked": [{"id": pool[0]["id"], "score": 0.1, "reason": "无关"}]})

    with patch(f"{HT}.settings", _settings(hot_topic_min_score=0.3)), \
         patch(f"{HT}.is_llm_configured", return_value=True), \
         patch(f"{HT}.read_agent_context", return_value=_memory_ctx("用户喜欢文学")), \
         patch(f"{HT}.generate_completion", return_value=ranked):
        result = select_hot_topic_candidate(account_id="acc-ht-low", now=now)

    assert result["action"] == "no_op"
    assert result["reason"] == "hot_topic_below_min_score"


# --------------------------------------------------------------------------- planning 接入
def test_plan_hot_topic_fallback_when_topic_and_content_empty(fresh_db):
    from app.proactive.orchestration.planning import plan_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-plan-ht")
    _create_route("acc-plan-ht")
    ensure_account_state(account_id="acc-plan-ht")

    def empty_topic(*, account_id, now):
        return {"action": "no_op", "account_id": account_id, "reason": "llm_no_topic_followup_candidate"}

    def empty_content(*, account_id, now):
        return {"action": "no_op", "account_id": account_id, "reason": "llm_no_content_invitation"}

    def hot(*, account_id, now):
        return {
            "action": "hot_topic_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-hot-1",
                "type": "hot_topic",
                "topic": "露营",
                "text": "最近想去露营吗",
                "generated_at": "2026-07-01 10:00:00",
            },
        }

    result = plan_reactivation_candidate(
        account_id="acc-plan-ht",
        now=datetime(2026, 7, 1, 10, 0),
        topic_followup_generator=empty_topic,
        content_invitation_generator=empty_content,
        hot_topic_generator=hot,
    )
    candidate = get_reactivation_candidate(account_id="acc-plan-ht")

    assert result["reactivation_type"] == "hot_topic"
    assert candidate["type"] == "hot_topic"
    assert candidate["text"] == "最近想去露营吗"


def test_plan_hot_topic_not_run_when_topic_wins(fresh_db):
    from app.proactive.orchestration.planning import plan_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-plan-ht2")
    _create_route("acc-plan-ht2")
    ensure_account_state(account_id="acc-plan-ht2")
    calls = {"hot": 0}

    def topic(*, account_id, now):
        return {
            "action": "topic_followup_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-topic-1",
                "type": "topic_followup",
                "topic": "亲子",
                "text": "娃今天乖吗",
                "generated_at": "2026-07-01 10:00:00",
            },
        }

    def hot(*, account_id, now):
        calls["hot"] += 1
        return {"action": "no_op", "account_id": account_id, "reason": "should_not_run"}

    result = plan_reactivation_candidate(
        account_id="acc-plan-ht2",
        now=datetime(2026, 7, 1, 10, 0),
        topic_followup_generator=topic,
        content_invitation_generator=lambda **k: {"action": "no_op", "reason": "x", "account_id": k["account_id"]},
        hot_topic_generator=hot,
    )

    assert result["reactivation_type"] == "topic_followup"
    assert calls["hot"] == 0  # 高优先 topic 命中 → hot_topic proposer 短路不执行
    # 被抢占的 hot_topic_generation 为合成 no_op，reason 记明抢占者。
    assert result["hot_topic_generation"]["reason"] == "topic_followup_candidate_selected"
