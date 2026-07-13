from datetime import datetime
from unittest.mock import patch


from tests.factories import create_account as _create_account
from tests.factories import create_route as _create_route


def _insert_inbound(account_id: str, *, created_at: str) -> None:
    from app.db import connect, insert_message, list_sessions_for_account

    session = list_sessions_for_account(account_id=account_id, limit=1)[0]
    row_id = insert_message(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"inbound-{account_id}-{created_at}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="刚刚又聊了一句",
        raw={"source": "test"},
    )
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (created_at, row_id),
        )


def test_reactivation_candidate_upsert_replace_and_clear_preserves_metadata(fresh_db):
    from app.db import get_proactive_account_state, upsert_proactive_account_state
    from app.proactive.store.candidates import REACTIVATION_METADATA_KEY, clear_reactivation_candidate, get_reactivation_candidate, upsert_reactivation_candidate

    _create_account("acc-reactivation")
    upsert_proactive_account_state(
        account_id="acc-reactivation",
        metadata={"existing": "keep"},
    )

    first_state = upsert_reactivation_candidate(
        account_id="acc-reactivation",
        candidate={
            "id": "react-1",
            "type": "topic_followup",
            "topic": "相亲聊天压力",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "reason": "用户昨天反复讨论相亲回复压力",
            "confidence": 0.9,
            "generated_at": "2026-06-05 10:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "source_message_cutoff_id": "123",
            "dedupe": {"checked": True, "duplicate": False},
        },
    )
    second_state = upsert_reactivation_candidate(
        account_id="acc-reactivation",
        candidate={
            "id": "react-2",
            "type": "topic_followup",
            "text": "昨晚小家伙睡得乖不乖？",
        },
    )
    candidate = get_reactivation_candidate(account_id="acc-reactivation")
    cleared = clear_reactivation_candidate(
        account_id="acc-reactivation",
        reason="sent",
        now=datetime(2026, 6, 5, 12, 15),
    )

    assert first_state["metadata"]["existing"] == "keep"
    assert first_state["metadata"][REACTIVATION_METADATA_KEY]["source_message_cutoff_id"] == 123
    assert second_state["metadata"][REACTIVATION_METADATA_KEY]["id"] == "react-2"
    assert candidate["text"] == "昨晚小家伙睡得乖不乖？"
    assert REACTIVATION_METADATA_KEY not in cleared["metadata"]
    assert cleared["metadata"]["existing"] == "keep"
    assert cleared["metadata"]["reactivation_candidate_cleared_reason"] == "sent"
    assert get_proactive_account_state(account_id="acc-reactivation")["metadata"]["existing"] == "keep"


def test_content_invitation_reactivation_requires_invitation_id():
    from app.proactive.store.candidates import normalize_reactivation_candidate

    try:
        normalize_reactivation_candidate(
            {
                "id": "react-content",
                "type": "content_invitation",
                "text": "要不要看几条中亚五国相关内容？",
            }
        )
    except ValueError as exc:
        assert "content_invitation_id" in str(exc)
    else:
        raise AssertionError("content_invitation candidate without id should fail")


def test_reactivation_outbound_metadata_includes_dedupe_and_type():
    from app.proactive.store.candidates import reactivation_outbound_metadata

    metadata = reactivation_outbound_metadata(
        candidate={
            "id": "react-3",
            "type": "content_invitation",
            "content_invitation_id": "cinv-1",
            "topic": "中亚五国轻知识",
            "text": "Mark，要不要看几条中亚五国相关内容？",
            "generated_at": "2026-06-05 10:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "dedupe": {"checked": True, "duplicate": False},
        },
        sent_at=datetime(2026, 6, 5, 12, 15, 3),
        extra={"policy": {"daily_limit_key": "reactivation"}},
    )

    assert metadata["reactivation"] is True
    assert metadata["reactivation_type"] == "content_invitation"
    assert metadata["content_invitation_id"] == "cinv-1"
    assert metadata["dedupe"]["duplicate"] is False
    assert metadata["sent_at"] == "2026-06-05 12:15:03"
    assert metadata["policy"]["daily_limit_key"] == "reactivation"


def test_reactivation_outbound_queries_match_text_true_and_keep_account_scope(fresh_db):
    from app.db import (
        count_reactivation_outbound_for_quota_date,
        create_outbound_message,
        list_reactivation_outbound_messages_admin,
        list_recent_reactivation_outbound_messages,
    )

    _create_account("acc-react-meta")
    _create_account("acc-react-other")

    bool_row = create_outbound_message(
        account_id="acc-react-meta",
        channel="openclaw-weixin",
        channel_account_id=None,
        to_user_id="user@im.wechat",
        session_key=None,
        source="reactivation",
        text="bool true",
        idempotency_key="react-meta-bool",
        quota_date="2026-06-24",
        metadata={"reactivation": True},
    )
    text_row = create_outbound_message(
        account_id="acc-react-meta",
        channel="openclaw-weixin",
        channel_account_id=None,
        to_user_id="user@im.wechat",
        session_key=None,
        source="reactivation",
        text="text true",
        idempotency_key="react-meta-text",
        quota_date="2026-06-24",
        metadata={"reactivation": "true"},
    )
    create_outbound_message(
        account_id="acc-react-meta",
        channel="openclaw-weixin",
        channel_account_id=None,
        to_user_id="user@im.wechat",
        session_key=None,
        source="reactivation",
        text="false flag",
        idempotency_key="react-meta-false",
        quota_date="2026-06-24",
        metadata={"reactivation": False},
    )
    create_outbound_message(
        account_id="acc-react-other",
        channel="openclaw-weixin",
        channel_account_id=None,
        to_user_id="user@im.wechat",
        session_key=None,
        source="reactivation",
        text="other account",
        idempotency_key="react-meta-other",
        quota_date="2026-06-24",
        metadata={"reactivation": True},
    )

    assert count_reactivation_outbound_for_quota_date(
        account_id="acc-react-meta",
        quota_date="2026-06-24",
    ) == 2
    recent_ids = [
        item["id"]
        for item in list_recent_reactivation_outbound_messages(
            account_id="acc-react-meta",
            since="2000-01-01 00:00:00",
        )
    ]
    admin_ids = [
        item["id"]
        for item in list_reactivation_outbound_messages_admin(
            account_id="acc-react-meta",
            since="2000-01-01 00:00:00",
        )
    ]

    assert recent_ids == [text_row["id"], bool_row["id"]]
    assert admin_ids == [text_row["id"], bool_row["id"]]


def test_dispatch_reactivation_dry_run_would_send_without_outbound(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-dispatch")
    _create_route("acc-react-dispatch")
    ensure_account_state(account_id="acc-react-dispatch")
    upsert_reactivation_candidate(
        account_id="acc-react-dispatch",
        candidate={
            "id": "react-send-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "dedupe": {"checked": True, "duplicate": False},
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-dispatch",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    assert result["action"] == "would_send"
    assert result["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert result["outbound_metadata"]["reactivation"] is True
    assert result["outbound_metadata"]["dry_run"] is True
    assert list_outbound_messages(account_id="acc-react-dispatch") == []


def test_dispatch_reactivation_real_send_uses_reactivation_category_for_quota_and_dedupe(fresh_db):
    from app.db import (
        count_reactivation_outbound_for_quota_date,
        list_outbound_messages,
        list_recent_reactivation_outbound_messages,
    )
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-real")
    _create_route("acc-react-real")
    ensure_account_state(account_id="acc-react-real")
    upsert_reactivation_candidate(
        account_id="acc-react-real",
        candidate={
            "id": "react-real-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:react-real-1"},
    ):
        first = dispatch_reactivation_candidate(
            account_id="acc-react-real",
            now=datetime(2026, 6, 5, 12, 15),
            dry_run=False,
            dedupe_checker=lambda **kwargs: {
                "checked": True,
                "duplicate": False,
                "reason": "test",
            },
        )

    outbound = list_outbound_messages(account_id="acc-react-real", limit=10)
    recent_history = list_recent_reactivation_outbound_messages(
        account_id="acc-react-real",
        since="2000-01-01 00:00:00",
    )

    assert first["action"] == "sent"
    # 拉活话题唤回已并入 companion_followup；拉活来源由 metadata.reactivation 标识。
    assert first["outbound_message"]["product_category"] == "companion_followup"
    assert outbound[0]["product_category"] == "companion_followup"
    assert count_reactivation_outbound_for_quota_date(
        account_id="acc-react-real",
        quota_date="2026-06-05",
    ) == 1
    assert [item["id"] for item in recent_history] == [outbound[0]["id"]]
    assert get_reactivation_candidate(account_id="acc-react-real") is None

    upsert_reactivation_candidate(
        account_id="acc-react-real",
        candidate={
            "id": "react-real-2",
            "type": "content_invitation",
            "text": "要不要看看几条中亚五国内容？",
            "content_invitation_id": "cinv-react-real-2",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )
    second = dispatch_reactivation_candidate(
        account_id="acc-react-real",
        now=datetime(2026, 6, 5, 18, 15),
        dry_run=False,
    )

    assert second["action"] == "no_op"
    assert second["reason"] == "reactivation_daily_limit_already_sent"
    assert count_reactivation_outbound_for_quota_date(
        account_id="acc-react-real",
        quota_date="2026-06-05",
    ) == 1


def test_dispatch_reactivation_skips_when_touch_stale_at_send_time(fresh_db):
    """候选生成后经 avoidance/失败改期跨过 24 小时窗口：真正派发前必须重新判定 touch_state，
    不能只信任生成时的判断（见 dispatch_reactivation_candidate 里新增的复核）。"""
    from datetime import timedelta

    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.time_utils import beijing_naive_now

    _create_account("acc-react-stale")
    _create_route("acc-react-stale")
    ensure_account_state(account_id="acc-react-stale")
    now0 = beijing_naive_now()
    upsert_reactivation_candidate(
        account_id="acc-react-stale",
        candidate={
            "id": "react-stale-1",
            "type": "topic_followup",
            "text": "很久没聊了，看看你最近怎么样。",
            "scheduled_slot": "slot_1",
            "scheduled_at": now0.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )

    with patch("app.proactive.delivery.outbound.send_weixin_text") as mock_send:
        result = dispatch_reactivation_candidate(
            account_id="acc-react-stale",
            now=now0 + timedelta(hours=25),
            dry_run=False,
        )

    assert result["action"] == "no_op"
    assert result["reason"] == "proactive_touch_stale"
    assert get_reactivation_candidate(account_id="acc-react-stale") is None
    mock_send.assert_not_called()


def test_dispatch_reactivation_content_invitation_marks_row_invited(fresh_db):
    from app.db import (
        create_content_invitation,
        get_content_invitation,
        list_outbound_messages,
    )
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-ci")
    _create_route("acc-react-ci")
    ensure_account_state(account_id="acc-react-ci")
    invitation = create_content_invitation(
        account_id="acc-react-ci",
        topic="中亚五国",
        invitation_text="要不要看看几条中亚五国的内容？",
        title_items=[{"title": "标题一"}, {"title": "标题二"}, {"title": "标题三"}],
        expires_at="2099-01-01 00:00:00",
    )
    upsert_reactivation_candidate(
        account_id="acc-react-ci",
        candidate={
            "id": "react-ci-1",
            "type": "content_invitation",
            "text": "要不要看看几条中亚五国的内容？",
            "content_invitation_id": invitation["id"],
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:react-ci-1"},
    ):
        result = dispatch_reactivation_candidate(
            account_id="acc-react-ci",
            now=datetime(2026, 6, 5, 18, 15),
            dry_run=False,
            dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
        )

    outbound = list_outbound_messages(account_id="acc-react-ci", limit=10)
    updated = get_content_invitation(invitation_id=invitation["id"])

    assert result["action"] == "sent"
    # 拉活内容唤回已并入 content_invitation；拉活来源由 metadata.reactivation 标识。
    assert outbound[0]["product_category"] == "content_invitation"
    assert outbound[0]["metadata"]["reactivation"] is True
    # Reactivation owns advancing the content_invitations state machine so the
    # downstream "send titles" interaction stays available.
    assert updated["status"] == "invited"
    assert updated["outbound_message_id"] == outbound[0]["id"]
    assert get_reactivation_candidate(account_id="acc-react-ci") is None


def test_remote_content_invitation_finalizes_on_node_result(fresh_db, monkeypatch):
    from app.config import Settings
    from app.db import (
        create_content_invitation,
        get_content_invitation,
        list_outbound_messages,
    )
    from app.proactive.delivery import outbound as messaging
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state
    from app.routers.bridge import node_outbound_result
    from app.schemas import NodeOutboundResultRequest

    _create_account("acc-react-ci-remote")
    _create_route("acc-react-ci-remote")
    ensure_account_state(account_id="acc-react-ci-remote")
    monkeypatch.setattr(
        messaging,
        "settings",
        Settings(
            ai4all_role="central",
            default_node_id="aliyun2",
            local_node_inline_dispatch=False,
        ),
    )
    monkeypatch.setattr(
        messaging,
        "send_weixin_text",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("should not inline send")),
    )

    invitation = create_content_invitation(
        account_id="acc-react-ci-remote",
        topic="中亚五国",
        invitation_text="要不要看看几条中亚五国的内容？",
        title_items=[{"title": "标题一"}, {"title": "标题二"}, {"title": "标题三"}],
        expires_at="2099-01-01 00:00:00",
    )
    upsert_reactivation_candidate(
        account_id="acc-react-ci-remote",
        candidate={
            "id": "react-ci-remote",
            "type": "content_invitation",
            "text": "要不要看看几条中亚五国的内容？",
            "content_invitation_id": invitation["id"],
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-ci-remote",
        now=datetime(2026, 6, 5, 18, 15),
        dry_run=False,
        dedupe_checker=lambda **kwargs: {
            "checked": True,
            "duplicate": False,
            "reason": "test",
        },
    )
    outbound = list_outbound_messages(account_id="acc-react-ci-remote", limit=10)[0]
    claimed = get_content_invitation(invitation_id=invitation["id"])

    assert result["action"] == "queued"
    assert outbound["status"] == "pending"
    assert outbound["node_id"] == "aliyun2"
    assert claimed["status"] == "sending"
    assert get_reactivation_candidate(account_id="acc-react-ci-remote") is not None

    node_outbound_result(
        outbound["id"],
        NodeOutboundResultRequest(status="sent", gateway_message_id="remote-msg-1"),
    )
    updated = get_content_invitation(invitation_id=invitation["id"])

    assert updated["status"] == "invited"
    assert updated["outbound_message_id"] == outbound["id"]
    assert get_reactivation_candidate(account_id="acc-react-ci-remote") is None


def test_dispatch_reactivation_content_invitation_missing_row_clears_candidate(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-ci-missing")
    _create_route("acc-react-ci-missing")
    ensure_account_state(account_id="acc-react-ci-missing")
    upsert_reactivation_candidate(
        account_id="acc-react-ci-missing",
        candidate={
            "id": "react-ci-missing",
            "type": "content_invitation",
            "text": "要不要看看几条内容？",
            "content_invitation_id": "cinv-does-not-exist",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-ci-missing",
        now=datetime(2026, 6, 5, 18, 15),
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    assert result["action"] == "no_op"
    assert result["reason"] == "content_invitation_not_claimable"
    assert list_outbound_messages(account_id="acc-react-ci-missing") == []
    assert get_reactivation_candidate(account_id="acc-react-ci-missing") is None


def test_dispatch_reactivation_cancels_when_inbound_since_candidate(fresh_db):
    """候选生成后用户又说过话 → 取消推送并清除候选（取代旧的改期逻辑）。"""
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-cancel")
    _create_route("acc-react-cancel")
    ensure_account_state(account_id="acc-react-cancel")
    # 候选生成于 11:00，用户 11:45 又有入站 → 12:15 发送时应取消
    _insert_inbound("acc-react-cancel", created_at="2026-06-05 11:45:00")
    upsert_reactivation_candidate(
        account_id="acc-react-cancel",
        candidate={
            "id": "react-cancel-1",
            "type": "topic_followup",
            "text": "昨晚小家伙睡得乖不乖？",
            "generated_at": "2026-06-05 11:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-cancel",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
    )
    candidate = get_reactivation_candidate(account_id="acc-react-cancel")

    assert result["action"] == "no_op"
    assert result["reason"] == "inbound_since_candidate"
    assert candidate is None


def test_dispatch_reactivation_keeps_candidate_when_inbound_before_generation(fresh_db):
    """生成前/同秒的入站不应取消候选。

    时区一致性回归：曾用 local_to_utc_string 把 generated_at -8h，导致生成前 8 小时内
    的入站被误算为「生成后」而误清候选；`>=` 还会把同秒源消息算进去。
    """
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-before")
    _create_route("acc-react-before")
    ensure_account_state(account_id="acc-react-before")
    # 一条在生成前 30 分钟（10:30），一条与生成同秒（11:00:00）——都不应触发取消。
    _insert_inbound("acc-react-before", created_at="2026-06-05 10:30:00")
    _insert_inbound("acc-react-before", created_at="2026-06-05 11:00:00")
    upsert_reactivation_candidate(
        account_id="acc-react-before",
        candidate={
            "id": "react-before-1",
            "type": "topic_followup",
            "text": "昨晚睡得好吗？",
            "generated_at": "2026-06-05 11:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-before",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )
    candidate = get_reactivation_candidate(account_id="acc-react-before")

    # 未被误清 → dry_run 下走到 would_send，候选仍在
    assert result["action"] == "would_send"
    assert candidate is not None


def test_dispatch_reactivation_cancels_on_duplicate(fresh_db):
    """规则去重命中重复 → 取消并清除候选（无 LLM、无重生成）。"""
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-dup")
    _create_route("acc-react-dup")
    ensure_account_state(account_id="acc-react-dup")
    # 生成于 12:10、无后续入站 → 不会被 inbound 取消，进入去重判定
    upsert_reactivation_candidate(
        account_id="acc-react-dup",
        candidate={
            "id": "react-dup",
            "type": "topic_followup",
            "text": "昨天相亲对象后来有找你吗？",
            "generated_at": "2026-06-05 12:10:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-dup",
        now=datetime(2026, 6, 5, 12, 15),
        dry_run=True,
        dedupe_checker=lambda **kwargs: {
            "checked": True,
            "duplicate": True,
            "reason": "duplicate_topic",
        },
    )
    candidate = get_reactivation_candidate(account_id="acc-react-dup")

    assert result["action"] == "no_op"
    assert result["reason"] == "dedupe_duplicate"
    assert candidate is None


def test_rule_reactivation_dedupe_check_matches_exact_topic(fresh_db):
    """规则去重：与近 N 天已发的相同 topic 精确匹配即判重复，无 LLM。"""
    from app.proactive.delivery.outbound import send_proactive_text
    from app.proactive.store.candidates import rule_reactivation_dedupe_check
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-rule-dedupe")
    _create_route("acc-rule-dedupe")
    ensure_account_state(account_id="acc-rule-dedupe")
    now = datetime(2026, 6, 5, 12, 0)
    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:rule-dedupe"},
    ):
        send_proactive_text(
            account_id="acc-rule-dedupe",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@im.wechat",
            session_key="session-acc-rule-dedupe",
            source="reactivation",
            text="上次说的露营计划定下来了吗？",
            idempotency_key="rule-dedupe-sent",
            now=now,
            product_category="companion_followup",
            metadata={"topic": "露营计划", "reactivation": True},
        )

    duplicate = rule_reactivation_dedupe_check(
        account_id="acc-rule-dedupe",
        candidate={"topic": "露营计划", "text": "换个问法但同主题"},
        now=now,
    )
    fresh = rule_reactivation_dedupe_check(
        account_id="acc-rule-dedupe",
        candidate={"topic": "周末安排", "text": "完全不同的话题"},
        now=now,
    )

    assert duplicate["duplicate"] is True
    assert duplicate["reason"] == "duplicate_topic"
    assert fresh["duplicate"] is False


def test_dispatch_due_reactivation_sweep_sends_due_candidates_only(fresh_db):
    from app.db import list_outbound_messages
    from app.proactive.delivery.dispatch import dispatch_due_reactivation_candidates
    from app.proactive.store.candidates import upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-sweep")
    _create_route("acc-react-sweep")
    ensure_account_state(account_id="acc-react-sweep")
    upsert_reactivation_candidate(
        account_id="acc-react-sweep",
        candidate={
            "id": "react-sweep-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    # Before the slot: not due -> the sweep does not pick it up at all.
    early = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 11, 0),
        limit=10,
        dispatch_enabled=True,
        dry_run=True,
    )
    assert early == []

    # Kill-switch off: nothing dispatched even when due.
    off = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 12, 15),
        limit=10,
        dispatch_enabled=False,
        dry_run=False,
    )
    assert off == []

    # At the slot, dry-run: would_send, no real outbound created.
    results = dispatch_due_reactivation_candidates(
        now=datetime(2026, 6, 5, 12, 15),
        limit=10,
        dispatch_enabled=True,
        dry_run=True,
    )
    assert len(results) == 1
    assert results[0]["action"] == "would_send"
    assert results[0]["text"] == "昨天那个相亲对象后来有再找你吗？"
    assert list_outbound_messages(account_id="acc-react-sweep") == []


def test_scan_due_does_not_overwrite_existing_candidate(fresh_db):
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state, scan_due_proactive_account_checks

    _create_account("acc-react-notdue")
    _create_route("acc-react-notdue")
    ensure_account_state(
        account_id="acc-react-notdue",
        next_scan_at=datetime(2026, 6, 5, 12, 0),
    )
    upsert_reactivation_candidate(
        account_id="acc-react-notdue",
        candidate={
            "id": "react-notdue-1",
            "type": "topic_followup",
            "text": "昨天那个面试结果出来了吗？",
            "scheduled_slot": "slot_2",
            "scheduled_at": "2026-06-05 18:15:00",
        },
    )

    # Planning runs for a due account, but an existing queued candidate must be
    # preserved (the slot + send happen in the dispatch sweep), so the planning
    # generators must NOT run and the candidate must not change.
    def _explode_topic_followup(**_):
        raise AssertionError("topic_followup generator must not run when a candidate exists")

    def _explode_content(**_):
        raise AssertionError("content_invitation generator must not run when a candidate exists")

    # 猴补 planning 实际引用的生成器名（scan_due 在 orchestration.planning 里按模块全局
    # 查找并注入 plan），若规划误跑生成就会抛出——验证"有 pending 候选则跳过生成"。
    import app.proactive.orchestration.planning as planning

    original_topic = planning.generate_topic_followup_candidate
    original_content = planning.generate_content_invitation_candidate
    planning.generate_topic_followup_candidate = _explode_topic_followup
    planning.generate_content_invitation_candidate = _explode_content
    try:
        results = scan_due_proactive_account_checks(
            now=datetime(2026, 6, 5, 14, 0),
            limit=10,
        )
    finally:
        planning.generate_topic_followup_candidate = original_topic
        planning.generate_content_invitation_candidate = original_content

    assert results[0]["reactivation_planning"]["reason"] == "reactivation_candidate_pending"
    candidate = get_reactivation_candidate(account_id="acc-react-notdue")
    assert candidate["id"] == "react-notdue-1"
    assert candidate["scheduled_at"] == "2026-06-05 18:15:00"


def test_reactivation_slot_applies_send_jitter(fresh_db):
    from app.proactive.slots import next_reactivation_slot

    fresh_db.reactivation_send_jitter_min_seconds = 60
    fresh_db.reactivation_send_jitter_max_seconds = 120
    now = datetime(2026, 6, 5, 12, 0, 0)
    seen = set()
    for _ in range(25):
        slot = next_reactivation_slot(now=now)
        assert slot["scheduled_slot"] == "slot_1"
        t = datetime.fromisoformat(slot["scheduled_at"].replace(" ", "T"))
        # base slot 12:15:00 + forward jitter [60,120]s -> [12:16:00, 12:17:00]
        assert datetime(2026, 6, 5, 12, 16, 0) <= t <= datetime(2026, 6, 5, 12, 17, 0)
        seen.add(slot["scheduled_at"])
    assert len(seen) > 1  # offset is randomized, not constant


def test_upsert_proactive_account_state_metadata_patch_preserves_sibling_keys(fresh_db):
    from app.db import get_proactive_account_state, upsert_proactive_account_state

    _create_account("acc-patch")
    upsert_proactive_account_state(
        account_id="acc-patch",
        metadata={
            "commitments": [{"id": "c1"}],
            "reactivation_candidate": {"id": "r1", "type": "topic_followup", "text": "hi"},
            "other_key": "value",
        },
    )
    # Patch one key, set another, delete a third — siblings must survive.
    upsert_proactive_account_state(
        account_id="acc-patch",
        metadata_patch={
            "reactivation_candidate": {"id": "r2", "type": "topic_followup", "text": "hello"},
            "added_key": "added",
            "other_key": None,
        },
    )
    metadata = get_proactive_account_state(account_id="acc-patch")["metadata"]
    assert metadata["commitments"] == [{"id": "c1"}]
    assert metadata["reactivation_candidate"]["id"] == "r2"
    assert metadata["added_key"] == "added"
    assert "other_key" not in metadata


def test_plan_reactivation_persists_content_invitation_candidate(fresh_db):
    from app.proactive.orchestration.planning import plan_reactivation_candidate
    from app.proactive.store.candidates import REACTIVATION_TYPE_CONTENT_INVITATION, get_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-plan-content")
    ensure_account_state(account_id="acc-plan-content")

    def fake_content_generator(*, account_id, now):
        return {
            "action": "content_invitation_candidate_created",
            "account_id": account_id,
            "content_invitation": {
                "id": "cinv-plan-1",
                "topic": "中亚五国轻知识",
                "invitation_text": "Mark，要不要看几条中亚五国相关内容？",
                "title_items": [{"title": "标题一"}, {"title": "标题二"}, {"title": "标题三"}],
            },
            "evaluated_at": "2026-06-05 10:00:00",
        }

    result = plan_reactivation_candidate(
        account_id="acc-plan-content",
        now=datetime(2026, 6, 5, 10, 0),
        content_invitation_generator=fake_content_generator,
    )
    candidate = get_reactivation_candidate(account_id="acc-plan-content")

    assert result["action"] == "reactivation_candidate_planned"
    assert result["reactivation_type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    assert candidate["type"] == REACTIVATION_TYPE_CONTENT_INVITATION
    assert candidate["content_invitation_id"] == "cinv-plan-1"
    assert candidate["text"] == "Mark，要不要看几条中亚五国相关内容？"
    assert candidate["metadata"]["title_count"] == 3


def test_plan_reactivation_topic_followup_takes_priority(fresh_db):
    from app.proactive.orchestration.planning import plan_reactivation_candidate
    from app.proactive.store.candidates import REACTIVATION_TYPE_TOPIC_FOLLOWUP, get_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-plan-topic")
    ensure_account_state(account_id="acc-plan-topic")
    calls = {"content": 0}

    def fake_topic_generator(*, account_id, now):
        return {
            "action": "topic_followup_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-topic-1",
                "type": "topic_followup",
                "topic": "亲子陪伴",
                "text": "昨晚小家伙睡得乖不乖？",
                "generated_at": "2026-06-05 10:00:00",
            },
        }

    def fake_content_generator(*, account_id, now):
        calls["content"] += 1
        return {"action": "no_op", "account_id": account_id, "reason": "should_not_call"}

    result = plan_reactivation_candidate(
        account_id="acc-plan-topic",
        now=datetime(2026, 6, 5, 10, 0),
        topic_followup_generator=fake_topic_generator,
        content_invitation_generator=fake_content_generator,
    )
    candidate = get_reactivation_candidate(account_id="acc-plan-topic")

    assert calls["content"] == 0
    assert result["reactivation_type"] == REACTIVATION_TYPE_TOPIC_FOLLOWUP
    assert result["content_invitation_generation"]["reason"] == "topic_followup_candidate_selected"
    assert candidate["type"] == REACTIVATION_TYPE_TOPIC_FOLLOWUP
    assert candidate["text"] == "昨晚小家伙睡得乖不乖？"


def test_plan_new_user_reactivation_after_two_idle_hours(fresh_db):
    from app.db import connect, get_proactive_account_state, set_account_onboarding_state
    from app.proactive.orchestration.planning import (
        NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY,
        plan_new_user_reactivation_candidate,
    )
    from app.proactive.store.account_state import ensure_account_state
    from app.proactive.store.candidates import get_reactivation_candidate

    fresh_db.reactivation_send_jitter_min_seconds = 0
    fresh_db.reactivation_send_jitter_max_seconds = 0
    _create_account("acc-new-user-plan")
    _create_route("acc-new-user-plan")
    ensure_account_state(account_id="acc-new-user-plan")
    set_account_onboarding_state(account_id="acc-new-user-plan", state="complete")
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET created_at = ? WHERE id = ?",
            ("2026-06-05 09:00:00", "acc-new-user-plan"),
        )
        conn.execute(
            "UPDATE channel_bindings SET last_seen_at = ? WHERE account_id = ?",
            ("2026-06-05 10:00:00", "acc-new-user-plan"),
        )
    _insert_inbound(account_id="acc-new-user-plan", created_at="2026-06-05 10:00:00")

    def fake_topic_generator(*, account_id, now):
        return {
            "action": "topic_followup_candidate_created",
            "account_id": account_id,
            "reactivation_candidate": {
                "id": "react-new-user-topic-1",
                "type": "topic_followup",
                "topic": "破冰续聊",
                "text": "刚刚那个话题我还挺想听你多说两句的。",
                "generated_at": "2026-06-05 12:05:00",
            },
        }

    result = plan_new_user_reactivation_candidate(
        account_id="acc-new-user-plan",
        now=datetime(2026, 6, 5, 12, 5),
        topic_followup_generator=fake_topic_generator,
        hot_topic_generator=lambda **_: {"action": "no_op", "reason": "unused"},
    )
    candidate = get_reactivation_candidate(account_id="acc-new-user-plan")
    state = get_proactive_account_state(account_id="acc-new-user-plan")

    assert result["action"] == "reactivation_candidate_planned"
    assert result["reactivation_type"] == "topic_followup"
    assert candidate["scheduled_at"] == "2026-06-05 12:15:00"
    assert candidate["metadata"]["new_user_reactivation"] is True
    assert candidate["metadata"]["new_user_reactivation_eligibility"]["last_inbound_at"] == "2026-06-05 10:00:00"
    assert state["metadata"][NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY] == "2026-06-05 12:05:00"


def test_plan_new_user_reactivation_respects_six_hour_cooldown(fresh_db):
    from app.db import connect, set_account_onboarding_state
    from app.proactive.orchestration.planning import (
        NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY,
        plan_new_user_reactivation_candidate,
    )
    from app.proactive.store.account_state import ensure_account_state
    from app.proactive.store.candidates import get_reactivation_candidate

    _create_account("acc-new-user-cooldown")
    _create_route("acc-new-user-cooldown")
    ensure_account_state(
        account_id="acc-new-user-cooldown",
        metadata={NEW_USER_REACTIVATION_LAST_TRIGGERED_AT_KEY: "2026-06-05 12:00:00"},
    )
    set_account_onboarding_state(account_id="acc-new-user-cooldown", state="complete")
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET created_at = ? WHERE id = ?",
            ("2026-06-05 09:00:00", "acc-new-user-cooldown"),
        )
        conn.execute(
            "UPDATE channel_bindings SET last_seen_at = ? WHERE account_id = ?",
            ("2026-06-05 10:00:00", "acc-new-user-cooldown"),
        )
    _insert_inbound(account_id="acc-new-user-cooldown", created_at="2026-06-05 10:00:00")

    result = plan_new_user_reactivation_candidate(
        account_id="acc-new-user-cooldown",
        now=datetime(2026, 6, 5, 17, 59),
        topic_followup_generator=lambda **_: (_ for _ in ()).throw(AssertionError("should not generate")),
        hot_topic_generator=lambda **_: (_ for _ in ()).throw(AssertionError("should not generate")),
    )

    assert result["action"] == "no_op"
    assert result["reason"] == "new_user_reactivation_cooldown"
    assert result["metadata"]["eligibility"]["next_allowed_at"] == "2026-06-05 18:00:00"
    assert get_reactivation_candidate(account_id="acc-new-user-cooldown") is None


def test_new_user_hot_topic_dispatch_uses_independent_category(fresh_db):
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state
    from app.proactive.store.candidates import upsert_reactivation_candidate
    from app.db import list_outbound_messages

    fresh_db.proactive_quiet_hours_start = "00:00"
    fresh_db.proactive_quiet_hours_end = "00:00"
    fresh_db.hot_topic_dispatch_dry_run = True
    _create_account("acc-new-user-dispatch")
    _create_route("acc-new-user-dispatch")
    ensure_account_state(account_id="acc-new-user-dispatch")
    _insert_inbound(account_id="acc-new-user-dispatch", created_at="2026-06-05 10:00:00")
    upsert_reactivation_candidate(
        account_id="acc-new-user-dispatch",
        candidate={
            "id": "react-new-user-hot-1",
            "type": "hot_topic",
            "topic": "今日轻话题",
            "text": "刚看到个挺适合闲聊的小话题，想听听你怎么看。",
            "generated_at": "2026-06-05 11:00:00",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
            "metadata": {"new_user_reactivation": True},
        },
    )

    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        return_value={"messageId": "openclaw-weixin:new-user-hot"},
    ):
        result = dispatch_reactivation_candidate(
            account_id="acc-new-user-dispatch",
            now=datetime(2026, 6, 5, 12, 15),
            dry_run=False,
            dedupe_checker=lambda **_: {"duplicate": False},
        )
    outbound = list_outbound_messages(account_id="acc-new-user-dispatch")

    assert result["action"] == "sent"
    assert outbound[0]["source"] == "new_user_reactivation"
    assert outbound[0]["product_category"] == "new_user_reactivation"
    assert outbound[0]["metadata"]["reactivation_type"] == "hot_topic"


def _create_pending_reminder(account_id: str, *, due_at: str) -> None:
    """插入一条 pending 用户提醒，用于触发 policy 的 avoidance 拦截（bug B 复现）。"""
    from app.db import create_reminder

    create_reminder(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{account_id}",
        text="记得喝水",
        due_at=due_at,
    )


def test_dispatch_reactivation_reschedules_when_policy_cancels(fresh_db):
    """bug B：候选过了 reactivation 自身 60min avoidance，却被 policy 的 6h avoidance 取消时，
    不应原样留在过去的 slot 上每个 tick 空转，而应改期到下一个 slot。"""
    from app.db import list_outbound_messages
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-cancel")
    _create_route("acc-react-cancel")
    ensure_account_state(account_id="acc-react-cancel")
    # 提醒在 105 分钟后：落在 reactivation 60min 窗口之外、policy 6h 窗口之内。
    _create_pending_reminder("acc-react-cancel", due_at="2026-06-05 14:00:00")
    upsert_reactivation_candidate(
        account_id="acc-react-cancel",
        candidate={
            "id": "react-cancel-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )

    now = datetime(2026, 6, 5, 12, 15)
    result = dispatch_reactivation_candidate(
        account_id="acc-react-cancel",
        now=now,
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    # 被 policy 取消 → 改期，而非 send_blocked 且候选卡死。
    assert result["action"] == "delayed"
    candidate = get_reactivation_candidate(account_id="acc-react-cancel")
    assert candidate is not None
    new_scheduled = datetime.fromisoformat(candidate["scheduled_at"].replace(" ", "T"))
    assert new_scheduled > now  # scheduled_at 前移到未来，不再停在过去
    # 产生了一条 cancelled 出站行，原因是 policy 的 user-reminder avoidance。
    outbound = list_outbound_messages(account_id="acc-react-cancel", limit=10)
    assert outbound and outbound[0]["status"] == "cancelled"
    assert outbound[0]["policy_reason"] == "avoidance_window_user_reminder"

    # 同一 tick 再跑一次：scheduled_at 已在未来 → not_due，不再每 tick 空转。
    second = dispatch_reactivation_candidate(
        account_id="acc-react-cancel",
        now=now,
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )
    assert second["action"] == "not_due"


def test_dispatch_reactivation_clears_when_policy_cancels_at_final_slot(fresh_db):
    """bug B：最后一个 slot 被 policy 取消、已无下一 slot 时，应清除候选（等下次 planning 重生成），
    而不是把过期候选永远留着。"""
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-final")
    _create_route("acc-react-final")
    ensure_account_state(account_id="acc-react-final")
    # now=21:05（非静默时段），提醒在 105 分钟后（22:50）：reactivation 60min 外、policy 6h 内。
    _create_pending_reminder("acc-react-final", due_at="2026-06-05 22:50:00")
    upsert_reactivation_candidate(
        account_id="acc-react-final",
        candidate={
            "id": "react-final-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_3",
            "scheduled_at": "2026-06-05 21:05:00",
        },
    )

    result = dispatch_reactivation_candidate(
        account_id="acc-react-final",
        now=datetime(2026, 6, 5, 21, 5),
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )

    assert result["action"] == "send_blocked"
    assert get_reactivation_candidate(account_id="acc-react-final") is None


def test_dispatch_reactivation_reschedules_when_send_failed_downstream(fresh_db, monkeypatch):
    """failed 分支：下游拒收/限速（ret:-2 落 status='failed'）时，不应把过期候选原样留在
    过去的 slot 上每个 tick 重发（拉活日节流不数 failed，兜底闸拦不住 → 发送风暴），
    而应改期到下一个 slot，降为每-slot 重试。"""
    import app.proactive.delivery.outbound as outbound_mod
    from app.db import list_outbound_messages
    from app.openclaw_gateway import OpenClawRateLimited
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-failed")
    _create_route("acc-react-failed")
    ensure_account_state(account_id="acc-react-failed")
    upsert_reactivation_candidate(
        account_id="acc-react-failed",
        candidate={
            "id": "react-failed-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_1",
            "scheduled_at": "2026-06-05 12:15:00",
        },
    )
    # 关掉重试与退避，避免真实 sleep 拖慢测试。
    monkeypatch.setattr(outbound_mod.settings, "proactive_send_rate_limit_max_retries", 0, raising=False)
    monkeypatch.setattr(outbound_mod.settings, "proactive_send_rate_limit_backoff_seconds", 0.0, raising=False)

    now = datetime(2026, 6, 5, 12, 15)
    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        side_effect=OpenClawRateLimited("rate limited", ret=-2),
    ):
        result = dispatch_reactivation_candidate(
            account_id="acc-react-failed",
            now=now,
            dry_run=False,
            dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
        )

    # 下游拒收 → 改期，而非 send_blocked 且候选卡死。
    assert result["action"] == "delayed"
    candidate = get_reactivation_candidate(account_id="acc-react-failed")
    assert candidate is not None
    new_scheduled = datetime.fromisoformat(candidate["scheduled_at"].replace(" ", "T"))
    assert new_scheduled > now  # scheduled_at 前移到未来，不再停在过去
    # 产生了一条 failed 出站行（ret:-2 限速），policy_reason 为空（非策略拦截）。
    outbound = list_outbound_messages(account_id="acc-react-failed", limit=10)
    assert outbound and outbound[0]["status"] == "failed"
    assert outbound[0]["policy_reason"] is None
    assert (outbound[0]["error"] or "").startswith("rate_limited")

    # 同一 tick 再跑一次：scheduled_at 已在未来 → not_due，不再每 tick 空转重发。
    second = dispatch_reactivation_candidate(
        account_id="acc-react-failed",
        now=now,
        dry_run=False,
        dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
    )
    assert second["action"] == "not_due"
    # 且没有再产生新的出站行（仍只有第一条 failed）。
    assert len(list_outbound_messages(account_id="acc-react-failed", limit=10)) == 1


def test_dispatch_reactivation_clears_when_send_failed_at_final_slot(fresh_db, monkeypatch):
    """failed 分支：最后一个 slot 下游拒收、已无下一 slot 时，应清除候选（等下次 planning
    重生成），而不是把过期候选永远留着每 tick 重发。"""
    import app.proactive.delivery.outbound as outbound_mod
    from app.openclaw_gateway import OpenClawRateLimited
    from app.proactive.delivery.dispatch import dispatch_reactivation_candidate
    from app.proactive.store.candidates import get_reactivation_candidate, upsert_reactivation_candidate
    from app.proactive.store.account_state import ensure_account_state

    _create_account("acc-react-failed-final")
    _create_route("acc-react-failed-final")
    ensure_account_state(account_id="acc-react-failed-final")
    upsert_reactivation_candidate(
        account_id="acc-react-failed-final",
        candidate={
            "id": "react-failed-final-1",
            "type": "topic_followup",
            "text": "昨天那个相亲对象后来有再找你吗？",
            "scheduled_slot": "slot_3",
            "scheduled_at": "2026-06-05 21:05:00",
        },
    )
    monkeypatch.setattr(outbound_mod.settings, "proactive_send_rate_limit_max_retries", 0, raising=False)
    monkeypatch.setattr(outbound_mod.settings, "proactive_send_rate_limit_backoff_seconds", 0.0, raising=False)

    with patch(
        "app.proactive.delivery.outbound.send_weixin_text",
        side_effect=OpenClawRateLimited("rate limited", ret=-2),
    ):
        result = dispatch_reactivation_candidate(
            account_id="acc-react-failed-final",
            now=datetime(2026, 6, 5, 21, 5),
            dry_run=False,
            dedupe_checker=lambda **kwargs: {"checked": True, "duplicate": False, "reason": "test"},
        )

    assert result["action"] == "send_blocked"
    assert get_reactivation_candidate(account_id="acc-react-failed-final") is None
