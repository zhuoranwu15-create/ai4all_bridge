"""阶段2 选择层（selection/）与召回协议（generation.base）单元测试。

只测纯逻辑：ranker 顺序、selector 短路/全空、ProactiveCandidate round-trip、Recaller 协议。
不依赖 DB / settings（这些模块是纯模块）。行为等价的端到端验证由既有 test_reactivation /
test_account_checks 覆盖。
"""
from datetime import datetime

from app.products.zhaoxi.proactive.contract.candidate import ProactiveCandidate
from app.products.zhaoxi.proactive.selection.ranker import DISCRETIONARY_RANK, rank_index, ranked_kinds
from app.products.zhaoxi.proactive.selection.selector import Proposal, SelectionResult, select_first


def test_ranker_order_and_index():
    # hot_topic（近期热点，全局召回）为末位 fallback：个人续聊/内容邀请皆空才兜底。
    assert ranked_kinds() == ("topic_followup", "content_invitation", "hot_topic")
    assert DISCRETIONARY_RANK[0] == "topic_followup"
    assert rank_index("topic_followup") == 0
    assert rank_index("content_invitation") == 1
    assert rank_index("hot_topic") == 2
    # 未登记的种类排到最后，不抛错。
    assert rank_index("unknown_kind") == len(DISCRETIONARY_RANK)


def _cand(kind: str) -> ProactiveCandidate:
    return ProactiveCandidate(account_id="acc", kind=kind, text="hi", candidate_id=f"id-{kind}")


def test_select_first_picks_highest_priority_and_short_circuits():
    calls = []

    def topic():
        calls.append("topic")
        return Proposal(result={"action": "topic_followup_candidate_created"}, candidate=_cand("topic_followup"))

    def content():
        calls.append("content")  # 不应被调用（topic 已命中 → 短路）
        return Proposal(result={"action": "content_invitation_candidate_created"}, candidate=_cand("content_invitation"))

    result = select_first(ranked_kinds(), {"topic_followup": topic, "content_invitation": content})

    assert isinstance(result, SelectionResult)
    assert result.chosen_kind == "topic_followup"
    assert result.candidate.kind == "topic_followup"
    assert calls == ["topic"]  # content proposer 未执行
    assert "content_invitation" not in result.outcomes  # 被抢占未跑


def test_select_first_falls_back_to_next_kind():
    def topic():
        return Proposal(result={"action": "no_op", "reason": "llm_no_topic_followup_candidate"}, candidate=None)

    def content():
        return Proposal(result={"action": "content_invitation_candidate_created"}, candidate=_cand("content_invitation"))

    result = select_first(ranked_kinds(), {"topic_followup": topic, "content_invitation": content})

    assert result.chosen_kind == "content_invitation"
    assert result.candidate.kind == "content_invitation"
    # 两个 proposer 都跑过（topic 没命中）。
    assert set(result.outcomes) == {"topic_followup", "content_invitation"}


def test_select_first_returns_none_when_all_empty():
    def empty():
        return Proposal(result={"action": "no_op", "reason": "x"}, candidate=None)

    result = select_first(ranked_kinds(), {"topic_followup": empty, "content_invitation": empty})

    assert result.chosen_kind is None
    assert result.candidate is None
    assert set(result.outcomes) == {"topic_followup", "content_invitation"}


def test_proactive_candidate_round_trip_matches_normalize():
    from app.products.zhaoxi.proactive.store.candidates import normalize_reactivation_candidate

    topic_legacy = {
        "id": "reactivation-topic-x",
        "type": "topic_followup",
        "text": "昨天那个相亲对象后来有再找你吗？",
        "topic": "相亲聊天压力",
        "reason": "用户最近讨论相亲回复压力",
        "confidence": 0.91,
        "generated_at": "2026-06-05 10:00:00",
        "source_message_cutoff_id": 12,
    }
    content_legacy = {
        "id": "reactivation-content-7",
        "type": "content_invitation",
        "content_invitation_id": "7",
        "topic": "中亚五国",
        "text": "要不要看看几条中亚五国内容？",
        "generated_at": "2026-06-05 10:00:00",
        "metadata": {"title_count": 3, "source": "content_invitation_generation"},
    }
    for legacy in (topic_legacy, content_legacy):
        rt = ProactiveCandidate.from_legacy(legacy, account_id="acc").to_legacy()
        # round-trip 经 normalize 后与直接 normalize 等价（存储字节不变的保证）。
        assert normalize_reactivation_candidate(rt) == normalize_reactivation_candidate(legacy)


def test_recaller_protocol_runtime_checkable():
    from app.products.zhaoxi.proactive.recall.base import RecallContext, Recaller

    class _DummyRecaller:
        kind = "topic_followup"
        scope = "account"

        def propose(self, ctx):
            return []

    recaller = _DummyRecaller()
    assert isinstance(recaller, Recaller)  # 结构化协议（runtime_checkable）
    ctx = RecallContext(now=datetime(2026, 6, 5, 10, 0), account_id="acc")
    assert recaller.propose(ctx) == []
