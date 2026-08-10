"""产品级审核策略：Plum（海外）绝不打阿里云，国内产品行为保持不变。"""

from pathlib import Path
from unittest.mock import patch

from app.bootstrap.product_registry import MINGCHAN_APP_ID, PLUM_APP_ID, ZHAOXI_APP_ID


def _session(account_id: str, app_id: str):
    from app.db import connect, get_or_create_session

    with connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, app_id) VALUES (?, ?)",
            (account_id, app_id),
        )

    state = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )
    return state["session"]


def _message(*, account_id: str, app_id: str, content: str):
    from app.db import insert_message

    session = _session(account_id, app_id)
    message_db_id = insert_message(
        account_id=account_id,
        session_id=session["id"],
        message_id=f"msg-{account_id}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content=content,
        raw={},
    )
    assert message_db_id is not None
    return session, int(message_db_id)


# ---------------- resolve_moderation_policy ----------------

def test_domestic_products_read_global_switches(fresh_db):
    """朝夕/鸣蝉逐项回读全局开关，等价于产品级策略引入前的行为。"""

    fresh_db.moderation_enabled = True
    fresh_db.moderation_sync_guard_enabled = True
    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    fresh_db.moderation_image_safety_enabled = True
    fresh_db.moderation_llm_enabled = True
    from app.platform.moderation.product_policy import resolve_moderation_policy

    for app_id in (ZHAOXI_APP_ID, MINGCHAN_APP_ID):
        policy = resolve_moderation_policy(app_id)
        assert policy.enabled is True
        assert policy.sync_guard_enabled is True
        assert policy.aliyun_inbound_sync_enabled is True
        assert policy.aliyun_image_enabled is True
        assert policy.llm_enabled is True
        # None = 沿用全局词表
        assert policy.sensitive_terms_path is None


def test_domestic_aliyun_off_when_either_global_switch_off(fresh_db):
    """入站同步需总开关与入站开关同时为真。"""

    from app.platform.moderation.product_policy import resolve_moderation_policy

    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = False
    assert resolve_moderation_policy(ZHAOXI_APP_ID).aliyun_inbound_sync_enabled is False

    fresh_db.moderation_aliyun_enabled = False
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    assert resolve_moderation_policy(ZHAOXI_APP_ID).aliyun_inbound_sync_enabled is False


def test_plum_never_enables_external_providers_even_with_globals_on(fresh_db):
    """核心约束：Plum 不得被任何全局开关送入未审定的外部 provider。"""

    from app.platform.moderation.product_policy import resolve_moderation_policy

    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    fresh_db.moderation_image_safety_enabled = True
    fresh_db.moderation_llm_enabled = True
    policy = resolve_moderation_policy(PLUM_APP_ID)

    assert policy.enabled is True
    assert policy.aliyun_inbound_sync_enabled is False
    assert policy.aliyun_image_enabled is False
    assert policy.llm_enabled is False


def test_plum_keeps_shared_local_redlines(fresh_db):
    """海外 provider 未落地前，Plum 至少沿用共享确定性红线，不能形成空审核。"""

    from app.platform.moderation.sensitive_words import (
        check_sync_guard,
        clear_sensitive_terms_cache,
    )

    fresh_db.moderation_sensitive_terms_path = str(
        Path(__file__).resolve().parents[2] / "data/moderation/sensitive_terms.json"
    )
    clear_sensitive_terms_cache()

    decision = check_sync_guard(
        account_id="plum-redline-account",
        app_id=PLUM_APP_ID,
        text="诱导未成年裸聊",
        direction="outbound",
        content_kind="text",
        source_type="streaming_reply",
        source_id="turn-redline",
    )

    assert decision.allowed is False
    assert decision.level == "escalate"
    assert "minor_safety" in decision.categories
    clear_sensitive_terms_cache()


def test_unknown_app_id_falls_back_to_conservative(fresh_db):
    """未登记产品：不抛异常（抛异常在 turn engine 里等价 fail-open），回落保守档。"""

    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    fresh_db.moderation_image_safety_enabled = True
    fresh_db.moderation_llm_enabled = True
    from app.platform.moderation.product_policy import resolve_moderation_policy

    for unknown in ("no_such_product", "", None):
        policy = resolve_moderation_policy(unknown)
        assert policy.enabled is True          # 管线仍记录
        assert policy.sync_guard_enabled is True  # 本地规则仍跑
        assert policy.aliyun_inbound_sync_enabled is False  # 不调任何外部 provider
        assert policy.aliyun_image_enabled is False
        assert policy.llm_enabled is False


def test_test_only_product_is_not_registered_as_production_policy(fresh_db, caplog):
    """测试注入 app_id 也必须走未知产品保守档，不能污染生产策略表。"""

    from app.platform.moderation.product_policy import resolve_moderation_policy

    policy = resolve_moderation_policy("test_product")

    assert policy.aliyun_inbound_sync_enabled is False
    assert policy.aliyun_image_enabled is False
    assert policy.llm_enabled is False
    assert "moderation policy missing" in caplog.text


# ---------------- 端到端：入站筛查按产品分流 ----------------

def test_plum_inbound_skips_aliyun_and_still_creates_task(fresh_db):
    """Plum 入站：不调阿里云，但异步审核任务照建（管线可回溯）。"""

    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    from app.db import get_content_moderation_task_by_idempotency_key
    from app.platform.moderation.service import screen_inbound_message_sync

    session, mid = _message(
        account_id="plum-acc",
        app_id=PLUM_APP_ID,
        content="a perfectly normal message",
    )
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun",
        side_effect=AssertionError("plum must never hit aliyun"),
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="plum-acc",
            app_id=PLUM_APP_ID,
            session_id=session["id"],
            content_kind="text",
            text="a perfectly normal message",
        )

    assert decision.allowed is True
    assert decision.reason == "aliyun_inbound_sync_disabled"
    task = get_content_moderation_task_by_idempotency_key(
        app_id=PLUM_APP_ID,
        idempotency_key=f"message:plum-acc:{mid}:inbound"
    )
    assert task is not None
    assert task["metadata"]["app_id"] == PLUM_APP_ID


def test_zhaoxi_inbound_still_hits_aliyun(fresh_db):
    """同一开关下朝夕仍走阿里云，证明分流是按产品而非全局关停。"""

    fresh_db.moderation_aliyun_enabled = True
    fresh_db.moderation_aliyun_inbound_sync_enabled = True
    from app.platform.moderation.models import MachineReviewResult
    from app.platform.moderation.service import screen_inbound_message_sync

    calls = []

    def _fake(*, account_id, text, data_id):
        calls.append(account_id)
        return MachineReviewResult(
            reviewer_type="cloud",
            engine="aliyun_text_moderation_plus",
            engine_version="chat_detection_pro",
            level="pass",
        )

    session, mid = _message(
        account_id="zx-acc",
        app_id=ZHAOXI_APP_ID,
        content="今天天气真不错呀",
    )
    with patch(
        "app.platform.moderation.service.aliyun_review.review_text_with_aliyun", _fake
    ):
        decision = screen_inbound_message_sync(
            message_db_id=mid,
            account_id="zx-acc",
            app_id=ZHAOXI_APP_ID,
            session_id=session["id"],
            content_kind="text",
            text="今天天气真不错呀",
        )

    assert calls == ["zx-acc"]
    assert decision.allowed is True


# ---------------- 出站同步红线按产品分流 ----------------
