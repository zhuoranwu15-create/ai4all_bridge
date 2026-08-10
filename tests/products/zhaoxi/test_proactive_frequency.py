"""Phase 2 主动消息：每日上限用户覆盖 + 每周上限。"""
from datetime import datetime


from tests.factories import create_account as _create_account


def _seed_outbound(account_id, category, *, created_at, quota_date, i, metadata=None):
    """种一条 outbound，并把 created_at 改成指定值（滚动窗口计数用 created_at）。"""
    from app.db import connect, create_outbound_message

    row = create_outbound_message(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot",
        to_user_id="user",
        session_key=f"session-{account_id}",
        source="test",
        text="x",
        idempotency_key=f"{account_id}-{category}-{i}",
        quota_date=quota_date,
        product_category=category,
        metadata=metadata or {},
    )
    with connect() as conn:
        conn.execute(
            "UPDATE outbound_messages SET created_at = ? WHERE id = ?",
            (created_at, row["id"]),
        )
    return row


def _evaluate(account_id, category, now):
    from app.products.zhaoxi.proactive.delivery.policy import OutboundCategory, evaluate_outbound_policy

    return evaluate_outbound_policy(
        account_id=account_id,
        category=OutboundCategory(category),
        source="test",
        scheduled_at=None,
        now=now,
    )


NOW = datetime(2026, 5, 30, 10, 0)  # 白天，非静默
TODAY = "2026-05-30"
WITHIN_WEEK = "2026-05-29 12:00:00"  # 在 NOW-7天 窗口内


def test_daily_limit_blocks_without_override(fresh_db):
    """全局 companion 日上限=1：今天已发 1 条 → 第 2 条被全局拦。"""
    _create_account("acc-d0")
    _seed_outbound("acc-d0", "companion_followup", created_at=f"{TODAY} 09:00:00", quota_date=TODAY, i=1)
    decision = _evaluate("acc-d0", "companion_followup", NOW)
    assert decision.allowed is False
    assert decision.reason == "daily_limit_exceeded"


def test_user_daily_override_widens(fresh_db):
    """用户把 companion 每日上限放宽到 3：已发 2 条仍放行。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-d1")
    for i in range(2):
        _seed_outbound("acc-d1", "companion_followup", created_at=f"{TODAY} 0{i}:00:00", quota_date=TODAY, i=i)
    apply_proactive_message_settings_patch(
        account_id="acc-d1",
        patch={"frequency": {"companion_followup": {"max_per_day": 3}}},
        source="tool",
    )
    decision = _evaluate("acc-d1", "companion_followup", NOW)
    assert decision.allowed is True


def test_user_daily_override_tightens(fresh_db):
    """用户把 content_invitation 每日上限收紧到 1：已发 1 条 → 拦，reason=用户频次。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-d2")
    _seed_outbound("acc-d2", "content_invitation", created_at=f"{TODAY} 09:00:00", quota_date=TODAY, i=1)
    apply_proactive_message_settings_patch(
        account_id="acc-d2",
        patch={"frequency": {"content_invitation": {"max_per_day": 1}}},
        source="tool",
    )
    decision = _evaluate("acc-d2", "content_invitation", NOW)
    assert decision.allowed is False
    assert decision.reason == "proactive_user_frequency_exceeded"


def test_daily_override_clamped_to_hard_cap(fresh_db):
    """用户设 99/天 → 封顶到系统硬上限 3：已发 3 条即拦。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-d3")
    for i in range(3):
        _seed_outbound("acc-d3", "companion_followup", created_at=f"{TODAY} 0{i}:00:00", quota_date=TODAY, i=i)
    result = apply_proactive_message_settings_patch(
        account_id="acc-d3",
        patch={"frequency": {"companion_followup": {"max_per_day": 99}}},
        source="tool",
    )
    # 落库值被封顶
    assert result["settings"]["frequency"]["companion_followup"]["max_per_day"] == 3
    decision = _evaluate("acc-d3", "companion_followup", NOW)
    assert decision.allowed is False
    assert decision.reason == "proactive_user_frequency_exceeded"


def test_weekly_limit_blocks(fresh_db):
    """用户 companion 每周上限=2：近 7 天已发 2 条 → 拦。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-w1")
    # quota_date 用过去日期，避免触发当日日上限；created_at 落在 7 天窗口内
    for i in range(2):
        _seed_outbound("acc-w1", "companion_followup", created_at=WITHIN_WEEK, quota_date="2026-05-29", i=i)
    apply_proactive_message_settings_patch(
        account_id="acc-w1",
        patch={"frequency": {"companion_followup": {"max_per_day": 3, "max_per_week": 2}}},
        source="tool",
    )
    decision = _evaluate("acc-w1", "companion_followup", NOW)
    assert decision.allowed is False
    assert decision.reason == "proactive_user_frequency_exceeded"
    assert decision.counts.get("weekly_limit") == 2


def test_weekly_limit_content_invitation(fresh_db):
    """content_invitation 每周计数（拉活内容唤回已并入本分类）：近 7 天 2 条 → 第 3 条被周上限拦。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-w2")
    _seed_outbound("acc-w2", "content_invitation", created_at=WITHIN_WEEK, quota_date="2026-05-29", i=1)
    _seed_outbound("acc-w2", "content_invitation", created_at=WITHIN_WEEK, quota_date="2026-05-29", i=2)
    apply_proactive_message_settings_patch(
        account_id="acc-w2",
        patch={"frequency": {"content_invitation": {"max_per_day": 3, "max_per_week": 2}}},
        source="tool",
    )
    decision = _evaluate("acc-w2", "content_invitation", NOW)
    assert decision.allowed is False
    assert decision.reason == "proactive_user_frequency_exceeded"


def test_no_weekly_limit_when_unset(fresh_db):
    """未设每周上限 → 不做周判断（已发多条仍只受日上限约束）。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-w3")
    for i in range(5):
        _seed_outbound("acc-w3", "companion_followup", created_at=WITHIN_WEEK, quota_date="2026-05-29", i=i)
    # 只放宽日上限，不设周上限；今天 0 条 → 应放行
    apply_proactive_message_settings_patch(
        account_id="acc-w3",
        patch={"frequency": {"companion_followup": {"max_per_day": 3}}},
        source="tool",
    )
    decision = _evaluate("acc-w3", "companion_followup", NOW)
    assert decision.allowed is True


def test_total_per_day_blocks_across_categories(fresh_db):
    """total_per_day=2：companion 1条 + reactivation 1条 → 第3条被总量拦，提醒不计入。
    分类上限放宽到3，确保是总量拦截而非分类上限先触发。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-t1")
    _seed_outbound("acc-t1", "companion_followup", created_at=f"{TODAY} 08:00:00", quota_date=TODAY, i=1)
    _seed_outbound("acc-t1", "content_invitation", created_at=f"{TODAY} 09:00:00", quota_date=TODAY, i=2)
    # 种一条提醒——不应被计入总量
    _seed_outbound("acc-t1", "user_reminder", created_at=f"{TODAY} 07:00:00", quota_date=TODAY, i=3)
    apply_proactive_message_settings_patch(
        account_id="acc-t1",
        patch={
            "frequency": {
                "total_per_day": 2,
                # 分类上限放宽，确保总量是瓶颈
                "companion_followup": {"max_per_day": 3},
                "content_invitation": {"max_per_day": 3},
            }
        },
        source="tool",
    )
    decision = _evaluate("acc-t1", "companion_followup", NOW)
    assert decision.allowed is False
    assert decision.reason == "proactive_user_frequency_exceeded"
    assert decision.counts.get("total_daily_limit") == 2
    assert decision.counts.get("total_daily_count") == 2  # reminder 不计


def test_total_per_day_allows_when_under_limit(fresh_db):
    """total_per_day=3：今日只有1条主动消息 → 放行。"""
    from app.products.zhaoxi.proactive.preferences import apply_proactive_message_settings_patch

    _create_account("acc-t2")
    _seed_outbound("acc-t2", "companion_followup", created_at=f"{TODAY} 08:00:00", quota_date=TODAY, i=1)
    apply_proactive_message_settings_patch(
        account_id="acc-t2",
        patch={"frequency": {"total_per_day": 3}},
        source="tool",
    )
    decision = _evaluate("acc-t2", "content_invitation", NOW)
    assert decision.allowed is True
