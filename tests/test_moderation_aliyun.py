from types import SimpleNamespace
from unittest.mock import patch

from app.platform.moderation.models import MachineReviewResult


def _session(account_id: str):
    from app.db import get_or_create_session

    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    return state["session"]


def _message(*, account_id: str, content: str, message_type: str = "text"):
    from app.db import insert_message

    session = _session(account_id)
    message_db_id = insert_message(
        account_id=account_id,
        session_id=session["id"],
        message_id=f"msg-{account_id}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type=message_type,
        content=content,
        raw={},
    )
    assert message_db_id is not None
    return session, int(message_db_id)


def _fake_response(data, *, status_code=200, code=200, message="OK"):
    return SimpleNamespace(
        status_code=status_code,
        body=SimpleNamespace(code=code, message=message, data=data),
    )


# ---------------- review_text_with_aliyun ----------------

def test_aliyun_review_parses_hit_and_maps_category(fresh_db):
    fresh_db.aliyun_access_key_id = "ak"
    fresh_db.aliyun_access_key_secret = "sk"
    from app.platform.moderation import aliyun_review

    data = {"riskLevel": "high", "result": [{"label": "pornographic_adult", "riskWords": "敏感片段"}]}
    client = SimpleNamespace(text_moderation_plus=lambda req: _fake_response(data))
    with patch("app.platform.moderation.aliyun_review._build_client", return_value=client):
        result = aliyun_review.review_text_with_aliyun(account_id="a", text="x", data_id="d")

    assert result.reviewer_type == "cloud"
    assert result.level == "block"  # high -> block
    assert "cloud:pornographic_adult" in result.categories
    assert "cat:sexual_content" in result.categories
    assert result.error is None


def test_aliyun_review_pass_when_none(fresh_db):
    fresh_db.aliyun_access_key_id = "ak"
    fresh_db.aliyun_access_key_secret = "sk"
    from app.platform.moderation import aliyun_review

    data = {"riskLevel": "none", "result": []}
    client = SimpleNamespace(text_moderation_plus=lambda req: _fake_response(data))
    with patch("app.platform.moderation.aliyun_review._build_client", return_value=client):
        result = aliyun_review.review_text_with_aliyun(account_id="a", text="x", data_id="d")

    assert result.level == "pass"
    assert result.categories == []
    assert result.error is None


def test_aliyun_review_treats_nonlabel_as_pass(fresh_db):
    """安全内容会返回占位标签 nonLabel（RiskLevel=none）；必须判为 pass，不能误拦正常对话。"""

    fresh_db.aliyun_access_key_id = "ak"
    fresh_db.aliyun_access_key_secret = "sk"
    from app.platform.moderation import aliyun_review

    data = {
        "riskLevel": "none",
        "result": [{"label": "nonLabel", "description": "未检测出风险"}],
    }
    client = SimpleNamespace(text_moderation_plus=lambda req: _fake_response(data))
    with patch("app.platform.moderation.aliyun_review._build_client", return_value=client):
        result = aliyun_review.review_text_with_aliyun(account_id="a", text="今天天气不错", data_id="d")

    assert result.level == "pass"
    assert result.categories == []
    assert result.error is None


def test_aliyun_review_redacts_account_id_in_raw(fresh_db):
    fresh_db.aliyun_access_key_id = "ak"
    fresh_db.aliyun_access_key_secret = "sk"
    from app.platform.moderation import aliyun_review

    data = {"riskLevel": "low", "accountId": "secret-account", "result": [{"label": "inappropriate_profanity"}]}
    client = SimpleNamespace(text_moderation_plus=lambda req: _fake_response(data))
    with patch("app.platform.moderation.aliyun_review._build_client", return_value=client):
        result = aliyun_review.review_text_with_aliyun(account_id="a", text="x", data_id="d")

    assert result.raw_result["data"]["accountId"] == "<redacted>"
    assert result.level == "review"  # low -> review


def test_aliyun_review_not_configured_returns_error(fresh_db):
    fresh_db.aliyun_access_key_id = ""
    fresh_db.aliyun_access_key_secret = ""
    from app.platform.moderation import aliyun_review

    result = aliyun_review.review_text_with_aliyun(account_id="a", text="x", data_id="d")
    assert result.level == "error"
    assert result.error == "moderation_aliyun_not_configured"


def test_aliyun_review_request_exception_returns_error(fresh_db):
    fresh_db.aliyun_access_key_id = "ak"
    fresh_db.aliyun_access_key_secret = "sk"
    from app.platform.moderation import aliyun_review

    def _boom(req):
        raise TimeoutError("read timeout")

    client = SimpleNamespace(text_moderation_plus=_boom)
    with patch("app.platform.moderation.aliyun_review._build_client", return_value=client):
        result = aliyun_review.review_text_with_aliyun(account_id="a", text="x", data_id="d")

    assert result.level == "error"
    assert result.error == "moderation_aliyun_request_failed"


# ---------------- screen_inbound_message_sync ----------------

def _cloud(level="pass", categories=None, error=None):
    return MachineReviewResult(
        reviewer_type="cloud",
        engine="aliyun_text_moderation_plus",
        engine_version="chat_detection_pro",
        level=level,
        categories=categories or [],
        error=error,
    )


def test_screen_pass_creates_machine_passed_task(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.db import list_content_moderation_results
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-pass", content="今天天气真不错呀")
    with patch("app.platform.moderation.service.aliyun_review.review_text_with_aliyun", return_value=_cloud("pass")):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-pass",
            session_id=session["id"],
            content_kind="text",
            text="今天天气真不错呀",
        )

    assert decision.allowed is True
    assert decision.level == "pass"
    results = list_content_moderation_results(task_id=decision.task_id)
    # 一条 rule + 一条 cloud
    assert {r["reviewer_type"] for r in results} == {"rule", "cloud"}


def test_screen_cloud_block_stops_reply_and_enters_review(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.db import get_content_moderation_task
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-block", content="一些命中云审核的内容")
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun",
        return_value=_cloud("block", ["cloud:pornographic_adult", "cat:sexual_content"]),
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-block",
            session_id=session["id"],
            content_kind="text",
            text="一些命中云审核的内容",
        )

    assert decision.allowed is False
    assert decision.level == "block"
    assert "cat:sexual_content" in decision.categories
    task = get_content_moderation_task(task_id=decision.task_id)
    assert task["status"] == "needs_review"


def test_screen_cloud_error_degrades_to_local_pass(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-degrade", content="普通的一句闲聊内容")
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun",
        return_value=_cloud("error", error="moderation_aliyun_request_failed"),
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-degrade",
            session_id=session["id"],
            content_kind="text",
            text="普通的一句闲聊内容",
        )

    assert decision.degraded is True
    assert decision.allowed is True  # 本地规则未命中 -> 放行
    assert decision.level == "pass"


def test_screen_cloud_error_degrades_to_local_rule_hit(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-degrade-hit", content="请检查 MODERATION_TEST_REVIEW 这条")
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun",
        return_value=_cloud("error", error="moderation_aliyun_request_failed"),
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-degrade-hit",
            session_id=session["id"],
            content_kind="text",
            text="请检查 MODERATION_TEST_REVIEW 这条",
        )

    assert decision.degraded is True
    assert decision.allowed is False  # 降级后本地规则命中 review
    assert decision.level == "review"


def test_screen_is_idempotent(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-idem", content="同一条消息重复筛查")
    with patch("app.platform.moderation.service.aliyun_review.review_text_with_aliyun", return_value=_cloud("pass")):
        first = screen_inbound_message_sync(
            message_db_id=mid, account_id="acc-idem", session_id=session["id"],
            content_kind="text", text="同一条消息重复筛查",
        )
        second = screen_inbound_message_sync(
            message_db_id=mid, account_id="acc-idem", session_id=session["id"],
            content_kind="text", text="同一条消息重复筛查",
        )

    assert first.task_id == second.task_id


def test_screen_disabled_falls_back_to_async_enqueue(fresh_db):
    fresh_db.moderation_aliyun_enabled = False
    from app.db import get_content_moderation_task_by_idempotency_key
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-async", content="阿里云关闭时应走异步路径")
    # 阿里云函数不应被调用
    with patch("app.platform.moderation.service.aliyun_review.review_text_with_aliyun", side_effect=AssertionError("should not call aliyun")):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-async",
            session_id=session["id"],
            content_kind="text",
            text="阿里云关闭时应走异步路径",
        )

    assert decision.allowed is True
    # 异步路径仍创建了任务
    task = get_content_moderation_task_by_idempotency_key(
        idempotency_key=f"message:acc-async:{mid}:inbound"
    )
    assert task is not None


def test_screen_account_isolation(fresh_db):
    fresh_db.moderation_aliyun_enabled = True
    from app.platform.moderation.service import screen_inbound_message_sync

    s1, m1 = _message(account_id="acc-x", content="账号X内容")
    s2, m2 = _message(account_id="acc-y", content="账号Y内容")
    with patch("app.platform.moderation.service.aliyun_review.review_text_with_aliyun", return_value=_cloud("pass")):
        d1 = screen_inbound_message_sync(
            message_db_id=m1, account_id="acc-x", session_id=s1["id"], content_kind="text", text="账号X内容",
        )
        d2 = screen_inbound_message_sync(
            message_db_id=m2, account_id="acc-y", session_id=s2["id"], content_kind="text", text="账号Y内容",
        )

    from app.db import get_content_moderation_task

    t1 = get_content_moderation_task(task_id=d1.task_id)
    t2 = get_content_moderation_task(task_id=d2.task_id)
    assert t1["account_id"] == "acc-x"
    assert t2["account_id"] == "acc-y"
    assert d1.task_id != d2.task_id


def test_screen_image_skips_moderation_entirely(fresh_db):
    """图片入站暂不接入审核：不调用阿里云、不建任何审核任务，直接放行。"""

    fresh_db.moderation_aliyun_enabled = True
    from app.db import get_content_moderation_task_by_idempotency_key, list_content_moderation_tasks
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-img", content="[图片]", message_type="image")
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun",
        side_effect=AssertionError("image must not hit sync cloud text review"),
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="acc-img",
            session_id=session["id"],
            content_kind="image",
            text="[图片]",
        )

    assert decision.allowed is True
    assert decision.reason == "inbound_non_text_skipped"
    # 不建任务：既无幂等任务，账号下也没有任何 moderation 任务
    assert get_content_moderation_task_by_idempotency_key(
        idempotency_key=f"message:acc-img:{mid}:inbound"
    ) is None
    assert list_content_moderation_tasks(account_id="acc-img", limit=20) == []


def test_screen_image_skips_even_when_aliyun_disabled(fresh_db):
    """阿里云入站同步关闭时，图片同样不建任务（不退回第一阶段异步图片审核）。"""

    fresh_db.moderation_aliyun_enabled = False
    from app.db import list_content_moderation_tasks
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(account_id="acc-img2", content="[图片]", message_type="image")
    decision = screen_inbound_message_sync(
        message_db_id=mid,
        account_id="acc-img2",
        session_id=session["id"],
        content_kind="image",
        text="[图片]",
    )

    assert decision.allowed is True
    assert decision.reason == "inbound_non_text_skipped"
    assert list_content_moderation_tasks(account_id="acc-img2", limit=20) == []


# ---------------- 失败率告警监控 ----------------

_THRESHOLDS = dict(
    window_seconds=300,
    failure_rate=0.2,
    min_samples=5,
    consecutive_threshold=3,
    cooldown_seconds=300,
)


def test_monitor_consecutive_failures_triggers():
    from app.platform.moderation.aliyun_alerting import AliyunFailureMonitor

    m = AliyunFailureMonitor()
    kw = {**_THRESHOLDS, "min_samples": 999}  # 排除失败率路径，仅看连续失败
    assert m.record(False, now=1.0, **kw) is None
    assert m.record(False, now=2.0, **kw) is None
    fired = m.record(False, now=3.0, **kw)
    assert fired is not None and fired[0] == "consecutive_failures"


def test_monitor_success_resets_consecutive():
    from app.platform.moderation.aliyun_alerting import AliyunFailureMonitor

    m = AliyunFailureMonitor()
    kw = {**_THRESHOLDS, "min_samples": 999}
    m.record(False, now=1.0, **kw)
    m.record(False, now=2.0, **kw)
    m.record(True, now=3.0, **kw)  # 重置
    assert m.record(False, now=4.0, **kw) is None
    assert m.record(False, now=5.0, **kw) is None
    assert m.record(False, now=6.0, **kw)[0] == "consecutive_failures"


def test_monitor_failure_rate_triggers():
    from app.platform.moderation.aliyun_alerting import AliyunFailureMonitor

    m = AliyunFailureMonitor()
    kw = {**_THRESHOLDS, "consecutive_threshold": 999}  # 排除连续失败路径
    # F,S,F,S,S -> 5 个样本，2 个失败 = 40% > 20%
    assert m.record(False, now=1.0, **kw) is None
    assert m.record(True, now=2.0, **kw) is None
    assert m.record(False, now=3.0, **kw) is None  # total=3 < min_samples
    assert m.record(True, now=4.0, **kw) is None  # total=4 < min_samples
    fired = m.record(True, now=5.0, **kw)
    assert fired is not None and fired[0] == "failure_rate"


def test_monitor_cooldown_suppresses_repeat():
    from app.platform.moderation.aliyun_alerting import AliyunFailureMonitor

    m = AliyunFailureMonitor()
    kw = {**_THRESHOLDS, "min_samples": 999}
    m.record(False, now=1.0, **kw)
    m.record(False, now=2.0, **kw)
    assert m.record(False, now=3.0, **kw)[0] == "consecutive_failures"
    # 冷却窗口内再次失败不应再次告警
    assert m.record(False, now=4.0, **kw) is None
    # 超过冷却后恢复告警
    assert m.record(False, now=400.0, **kw)[0] == "consecutive_failures"


def test_monitor_window_prunes_old_failures():
    from app.platform.moderation.aliyun_alerting import AliyunFailureMonitor

    m = AliyunFailureMonitor()
    kw = {**_THRESHOLDS, "consecutive_threshold": 999, "window_seconds": 100}
    # 老失败发生在窗口外，应被剔除，不计入失败率
    m.record(False, now=1.0, **kw)
    m.record(False, now=2.0, **kw)
    # now 推进超过窗口，后续全部成功
    for i, t in enumerate([200.0, 201.0, 202.0, 203.0, 204.0]):
        assert m.record(True, now=t, **kw) is None


def test_record_outcome_dispatches_when_webhook_set(fresh_db):
    from app.platform.moderation import aliyun_alerting

    fresh_db.feishu_alert_webhook_url = "https://example.com/open-apis/bot/v2/hook/abc"
    fresh_db.moderation_aliyun_alert_min_samples = 999  # 仅连续失败路径
    aliyun_alerting._monitor.reset()

    calls = []
    with patch.object(aliyun_alerting, "_dispatch_async", lambda url, msg, timeout: calls.append((url, msg))):
        aliyun_alerting.record_outcome(success=False, error="moderation_aliyun_request_failed", account_id="a")
        aliyun_alerting.record_outcome(success=False, error="moderation_aliyun_request_failed", account_id="a")
        aliyun_alerting.record_outcome(success=False, error="moderation_aliyun_request_failed", account_id="a")

    assert len(calls) == 1
    assert "consecutive_failures" in calls[0][1]


def test_record_outcome_no_dispatch_without_webhook(fresh_db):
    from app.platform.moderation import aliyun_alerting

    fresh_db.feishu_alert_webhook_url = ""
    fresh_db.moderation_aliyun_alert_min_samples = 999
    aliyun_alerting._monitor.reset()

    calls = []
    with patch.object(aliyun_alerting, "_dispatch_async", lambda url, msg, timeout: calls.append(url)):
        for _ in range(5):
            aliyun_alerting.record_outcome(success=False, error="x", account_id="a")

    assert calls == []


def test_record_outcome_disabled_skips(fresh_db):
    from app.platform.moderation import aliyun_alerting

    fresh_db.feishu_alert_webhook_url = "https://example.com/open-apis/bot/v2/hook/abc"
    fresh_db.moderation_aliyun_alert_enabled = False
    aliyun_alerting._monitor.reset()

    calls = []
    with patch.object(aliyun_alerting, "_dispatch_async", lambda url, msg, timeout: calls.append(url)):
        for _ in range(5):
            aliyun_alerting.record_outcome(success=False, error="x", account_id="a")

    assert calls == []
