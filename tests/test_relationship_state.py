"""Phase B：关系状态确定性更新（helpers + 纯函数 + turn/天级编排）。

见 docs/tech_design/relationship_state_implementation_plan_tmp.md §6/§7/§8.1。
"""
import asyncio
import itertools
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from app.products.zhaoxi.application.relationship import (
    RESOURCE_RISK_THRESHOLD_MICROS,
    apply_daily_deterministic_relationship,
    apply_daily_llm_relationship,
    compute_stage_threshold,
    compute_survival_status,
    maybe_update_relationship_state_after_turn,
    merge_llm_relationship,
)

_phone_seq = itertools.count(1)


from tests.factories import create_account as _create_account


def _add_inbound(*, account_id: str, session_id: int, created_at: str, idx: int) -> None:
    from app.db import connect, insert_message

    row_id = insert_message(
        account_id=account_id,
        session_id=session_id,
        message_id=f"m-{account_id}-{idx}",
        reply_to_message_id=None,
        direction="inbound",
        role="user",
        message_type="text",
        content="hi",
        raw={},
    )
    with connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE id = ?", (created_at, row_id)
        )


def _seed_inbound(*, account_id: str, session_id: int, count: int, date: str) -> None:
    for i in range(count):
        _add_inbound(
            account_id=account_id,
            session_id=session_id,
            created_at=f"{date} 10:{i % 60:02d}:{i // 60:02d}",
            idx=i,
        )


def _set_balance(*, account_id: str, micros: int) -> None:
    """为账号建立 platform_user + 绑定 + 钱包并把余额设为 micros。"""
    from app.db import connect, ensure_product_membership, ensure_wallet

    pu_id = f"pu-{account_id}"
    phone = f"1{next(_phone_seq):010d}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO platform_users(id, phone) VALUES (?, ?)", (pu_id, phone)
        )
        conn.execute(
            "INSERT INTO account_owner_bindings(platform_user_id, account_id, binding_method, status) "
            "VALUES (?, ?, 'manual', 'active')",
            (pu_id, account_id),
        )
    ensure_product_membership(platform_user_id=pu_id, app_id="zhaoxi")
    ensure_wallet(account_id=account_id, platform_user_id=pu_id)
    with connect() as conn:
        conn.execute(
            "UPDATE entitlement_wallets SET balance_shell_micros = ? WHERE account_id = ?",
            (micros, account_id),
        )


# ---------------------------------------------------------------------------
# 纯函数（B2）
# ---------------------------------------------------------------------------
def test_compute_stage_threshold():
    assert compute_stage_threshold(current_stage="icebreaking", inbound_count=30) == "icebreaking"
    assert compute_stage_threshold(current_stage="icebreaking", inbound_count=31) == "acquainted"
    # 不降级更高阶段
    assert compute_stage_threshold(current_stage="acquainted", inbound_count=0) == "acquainted"
    assert compute_stage_threshold(current_stage="deep_bond", inbound_count=0) == "deep_bond"


def _survival(cur, dates, bal=None, snap="2026-06-22"):
    return compute_survival_status(
        current_status=cur, inbound_dates=dates, snapshot_date=snap, balance_micros=bal
    )


def test_compute_survival_activity_rules():
    # 最近 2 自然日均有 inbound -> healthy
    assert _survival("cooling", ["2026-06-21", "2026-06-22"]) == "healthy"
    # healthy 且最近 3 自然日无 inbound（但 30 日内有）-> cooling
    assert _survival("healthy", ["2026-06-10"]) == "cooling"
    # 最近 30 自然日无 inbound -> inactive
    assert _survival("healthy", ["2026-05-01"]) == "inactive"
    assert _survival("cooling", []) == "inactive"
    # 无规则命中保留当前活跃态
    assert _survival("cooling", ["2026-06-19"]) == "cooling"
    assert _survival("healthy", ["2026-06-22"]) == "healthy"


def test_compute_survival_window_boundary():
    # 2 日窗口 = [06-21, 06-22] 闭区间；缺 06-21 不算连续 2 日
    assert _survival("cooling", ["2026-06-20", "2026-06-22"]) != "healthy"
    # 30 日窗口下界 = 05-24；05-24 命中则非 inactive
    assert _survival("cooling", ["2026-05-24"]) != "inactive"
    # 05-23 在窗口外 -> inactive
    assert _survival("cooling", ["2026-05-23"]) == "inactive"


def test_compute_survival_resource_risk_overlay():
    # 余额低于阈值 -> resource_risk（覆盖活跃态）
    assert _survival("healthy", ["2026-06-21", "2026-06-22"], bal=RESOURCE_RISK_THRESHOLD_MICROS - 1) == "resource_risk"
    # 等于阈值不触发
    assert _survival("cooling", ["2026-06-22"], bal=RESOURCE_RISK_THRESHOLD_MICROS) != "resource_risk"
    # 资源风险解除后按活跃天数重算（current=resource_risk 回退中性默认再算）
    assert _survival("resource_risk", ["2026-06-21", "2026-06-22"], bal=0) == "healthy"
    assert _survival("resource_risk", ["2026-06-19"], bal=0) == "cooling"
    # balance None 忽略资源风险
    assert _survival("cooling", ["2026-06-22"], bal=None) == "cooling"


# ---------------------------------------------------------------------------
# helpers（B1）
# ---------------------------------------------------------------------------
def test_count_inbound_messages_excludes_outbound(fresh_db):
    from app.db import connect, count_inbound_messages, insert_message

    session_id = _create_account("acc-count")
    _seed_inbound(account_id="acc-count", session_id=session_id, count=4, date="2026-06-01")
    out_id = insert_message(
        account_id="acc-count",
        session_id=session_id,
        message_id="out-1",
        reply_to_message_id=None,
        direction="outbound",
        role="assistant",
        message_type="text",
        content="hi",
        raw={},
    )
    assert out_id is not None

    assert count_inbound_messages(account_id="acc-count") == 4


def test_list_recent_inbound_dates_dedup_and_since(fresh_db):
    from app.db import list_recent_inbound_message_dates

    session_id = _create_account("acc-dates")
    _add_inbound(account_id="acc-dates", session_id=session_id, created_at="2026-06-01 10:00:00", idx=0)
    _add_inbound(account_id="acc-dates", session_id=session_id, created_at="2026-06-01 11:00:00", idx=1)
    _add_inbound(account_id="acc-dates", session_id=session_id, created_at="2026-06-03 09:00:00", idx=2)
    _add_inbound(account_id="acc-dates", session_id=session_id, created_at="2026-06-20 23:30:00", idx=3)

    assert list_recent_inbound_message_dates(account_id="acc-dates", since_date="2026-06-01") == [
        "2026-06-01",
        "2026-06-03",
        "2026-06-20",
    ]
    assert list_recent_inbound_message_dates(account_id="acc-dates", since_date="2026-06-02") == [
        "2026-06-03",
        "2026-06-20",
    ]


def test_get_wallet_balance_shell_micros(fresh_db):
    from app.db import get_wallet_balance_shell_micros

    _create_account("acc-bal-none")
    assert get_wallet_balance_shell_micros(account_id="acc-bal-none") is None

    _create_account("acc-bal-set")
    _set_balance(account_id="acc-bal-set", micros=-750_000)
    assert get_wallet_balance_shell_micros(account_id="acc-bal-set") == -750_000


# ---------------------------------------------------------------------------
# turn 级编排（§11.3）
# ---------------------------------------------------------------------------
def test_turn_30_stays_icebreaking(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-turn-30")
    _seed_inbound(account_id="acc-turn-30", session_id=session_id, count=30, date="2026-06-01")

    out = maybe_update_relationship_state_after_turn(account_id="acc-turn-30")

    assert out["changed"] is False
    assert get_account_user_meta(account_id="acc-turn-30") is None


def test_turn_31_promotes_acquainted(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-turn-31")
    _seed_inbound(account_id="acc-turn-31", session_id=session_id, count=31, date="2026-06-01")

    out = maybe_update_relationship_state_after_turn(account_id="acc-turn-31")

    assert out["changed"] is True
    assert get_account_user_meta(account_id="acc-turn-31")["relationship_stage"] == "acquainted"


def test_turn_deep_bond_not_downgraded(fresh_db):
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    session_id = _create_account("acc-turn-deep")
    update_account_user_meta_relationship(
        account_id="acc-turn-deep", relationship_stage="deep_bond"
    )
    _seed_inbound(account_id="acc-turn-deep", session_id=session_id, count=31, date="2026-06-01")

    maybe_update_relationship_state_after_turn(account_id="acc-turn-deep")

    assert get_account_user_meta(account_id="acc-turn-deep")["relationship_stage"] == "deep_bond"


def test_turn_resource_risk_on_low_balance(fresh_db):
    from app.db import get_account_user_meta

    _create_account("acc-turn-risk")
    _set_balance(account_id="acc-turn-risk", micros=RESOURCE_RISK_THRESHOLD_MICROS - 1)

    out = maybe_update_relationship_state_after_turn(account_id="acc-turn-risk")

    assert out["changed"] is True
    assert get_account_user_meta(account_id="acc-turn-risk")["agent_need_survival_status"] == "resource_risk"


def test_turn_does_not_touch_trust_growth(fresh_db):
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    session_id = _create_account("acc-turn-tg")
    update_account_user_meta_relationship(
        account_id="acc-turn-tg",
        agent_need_trust_status="stable",
        agent_need_growth_status="emerging",
    )
    _seed_inbound(account_id="acc-turn-tg", session_id=session_id, count=31, date="2026-06-01")

    maybe_update_relationship_state_after_turn(account_id="acc-turn-tg")

    meta = get_account_user_meta(account_id="acc-turn-tg")
    assert meta["relationship_stage"] == "acquainted"
    assert meta["agent_need_trust_status"] == "stable"
    assert meta["agent_need_growth_status"] == "emerging"


# ---------------------------------------------------------------------------
# 天级确定性编排（§11.4 确定性部分）
# ---------------------------------------------------------------------------
def _apply_daily(account_id: str, snapshot_date: str = "2026-06-22"):
    from app.db import get_account_user_meta

    return apply_daily_deterministic_relationship(
        account_id=account_id,
        snapshot_date=snapshot_date,
        current_meta=get_account_user_meta(account_id=account_id),
    )


def test_daily_two_consecutive_days_healthy(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-daily-healthy")
    _add_inbound(account_id="acc-daily-healthy", session_id=session_id, created_at="2026-06-21 10:00:00", idx=0)
    _add_inbound(account_id="acc-daily-healthy", session_id=session_id, created_at="2026-06-22 10:00:00", idx=1)

    ret = _apply_daily("acc-daily-healthy")

    assert ret["agent_need_survival_status"] == "healthy"
    assert get_account_user_meta(account_id="acc-daily-healthy")["agent_need_survival_status"] == "healthy"


def test_daily_healthy_then_three_empty_cooling(fresh_db):
    from app.db import update_account_user_meta_relationship

    session_id = _create_account("acc-daily-cool")
    update_account_user_meta_relationship(
        account_id="acc-daily-cool", agent_need_survival_status="healthy"
    )
    # 30 日内有 inbound（06-10），但最近 3 日（06-20..06-22）无
    _add_inbound(account_id="acc-daily-cool", session_id=session_id, created_at="2026-06-10 10:00:00", idx=0)

    ret = _apply_daily("acc-daily-cool")

    assert ret["agent_need_survival_status"] == "cooling"


def test_daily_thirty_days_empty_inactive(fresh_db):
    session_id = _create_account("acc-daily-inactive")
    # 仅一条远早于 30 日窗口的 inbound（窗口下界 05-24）
    _add_inbound(account_id="acc-daily-inactive", session_id=session_id, created_at="2026-05-01 10:00:00", idx=0)

    ret = _apply_daily("acc-daily-inactive")

    assert ret["agent_need_survival_status"] == "inactive"


def test_daily_resource_risk_does_not_block_other_states(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-daily-risk")
    _seed_inbound(account_id="acc-daily-risk", session_id=session_id, count=31, date="2026-06-01")
    _set_balance(account_id="acc-daily-risk", micros=RESOURCE_RISK_THRESHOLD_MICROS - 1)

    ret = _apply_daily("acc-daily-risk")

    # survival 为 resource_risk，但 stage 阈值照常计算、trust/growth 不受影响
    assert ret["agent_need_survival_status"] == "resource_risk"
    assert ret["relationship_stage"] == "acquainted"
    assert ret["agent_need_trust_status"] == "building"
    assert ret["agent_need_growth_status"] == "not_started"
    meta = get_account_user_meta(account_id="acc-daily-risk")
    assert meta["relationship_stage"] == "acquainted"
    assert meta["agent_need_survival_status"] == "resource_risk"


# ---------------------------------------------------------------------------
# 天级 LLM：merge 纯函数（C2）
# ---------------------------------------------------------------------------
def _det(stage="acquainted", survival="cooling", trust="building", growth="not_started"):
    return {
        "relationship_stage": stage,
        "agent_need_survival_status": survival,
        "agent_need_trust_status": trust,
        "agent_need_growth_status": growth,
    }


def test_merge_llm_stage_only_acquainted_to_deep_bond():
    # acquainted -> deep_bond 生效
    assert merge_llm_relationship(
        deterministic=_det("acquainted"), llm_out={"relationship_stage": "deep_bond"}
    )["relationship_stage"] == "deep_bond"
    # icebreaking 不被 LLM 越级
    assert merge_llm_relationship(
        deterministic=_det("icebreaking"), llm_out={"relationship_stage": "deep_bond"}
    )["relationship_stage"] == "icebreaking"
    # deep_bond 不被 LLM 降级
    assert merge_llm_relationship(
        deterministic=_det("deep_bond"), llm_out={"relationship_stage": "acquainted"}
    )["relationship_stage"] == "deep_bond"


def test_merge_llm_trust_growth_and_survival():
    # 合法 trust/growth 采用
    m = merge_llm_relationship(
        deterministic=_det(survival="resource_risk"),
        llm_out={"agent_need_trust_status": "stable", "agent_need_growth_status": "emerging"},
    )
    assert m["agent_need_trust_status"] == "stable"
    assert m["agent_need_growth_status"] == "emerging"
    # survival 恒取 deterministic，LLM 不参与
    assert m["agent_need_survival_status"] == "resource_risk"
    # None（信号不足）保留当前
    m2 = merge_llm_relationship(
        deterministic=_det(trust="building", growth="not_started"),
        llm_out={"agent_need_trust_status": None, "agent_need_growth_status": None},
    )
    assert m2["agent_need_trust_status"] == "building"
    assert m2["agent_need_growth_status"] == "not_started"


# ---------------------------------------------------------------------------
# 天级 LLM：apply_daily_llm_relationship（C2，patch generate_completion）
# ---------------------------------------------------------------------------
def _rel_json(stage="deep_bond", trust="stable", growth="emerging"):
    return json.dumps(
        {
            "relationship_stage": stage,
            "agent_need_trust_status": trust,
            "agent_need_growth_status": growth,
        }
    )


def test_apply_daily_llm_writes_and_keeps_survival(fresh_db):
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-llm-ok")
    update_account_user_meta_relationship(
        account_id="acc-llm-ok",
        relationship_stage="acquainted",
        agent_need_survival_status="cooling",
    )
    det = get_account_user_meta(account_id="acc-llm-ok")

    with patch("app.products.zhaoxi.application.relationship.generate_completion", return_value=_rel_json()):
        merged = apply_daily_llm_relationship(
            account_id="acc-llm-ok", deterministic=det, messages=[{"content": "hi"}]
        )

    assert merged["relationship_stage"] == "deep_bond"
    meta = get_account_user_meta(account_id="acc-llm-ok")
    assert meta["relationship_stage"] == "deep_bond"
    assert meta["agent_need_trust_status"] == "stable"
    assert meta["agent_need_growth_status"] == "emerging"
    # LLM 未触碰 survival（保持确定性 cooling）
    assert meta["agent_need_survival_status"] == "cooling"


def test_apply_daily_llm_invalid_output_keeps_current(fresh_db):
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-llm-bad")
    update_account_user_meta_relationship(
        account_id="acc-llm-bad", relationship_stage="acquainted"
    )
    det = get_account_user_meta(account_id="acc-llm-bad")

    # 非法枚举 -> parse 归一为 None -> merge 保留当前
    bad = json.dumps({"relationship_stage": "bogus", "agent_need_trust_status": "???"})
    with patch("app.products.zhaoxi.application.relationship.generate_completion", return_value=bad):
        merged = apply_daily_llm_relationship(
            account_id="acc-llm-bad", deterministic=det, messages=[{"content": "hi"}]
        )

    assert merged["relationship_stage"] == "acquainted"
    assert merged["agent_need_trust_status"] == "building"


def test_apply_daily_llm_failure_propagates(fresh_db):
    from app.db import get_account_user_meta, update_account_user_meta_relationship

    _create_account("acc-llm-raise")
    update_account_user_meta_relationship(
        account_id="acc-llm-raise", relationship_stage="acquainted"
    )
    det = get_account_user_meta(account_id="acc-llm-raise")

    with patch("app.products.zhaoxi.application.relationship.generate_completion", return_value="not json"):
        with pytest.raises(ValueError):
            apply_daily_llm_relationship(
                account_id="acc-llm-raise", deterministic=det, messages=[{"content": "hi"}]
            )


# ---------------------------------------------------------------------------
# 天级 LLM：scheduler run_once 接入（C3）
# ---------------------------------------------------------------------------
def _seed_companion_recent(account_id: str) -> None:
    """预置 companion 为近期已评估，使 run_once 跳过 companion LLM，仅触发关系 LLM。"""
    from app.db import upsert_account_user_meta

    upsert_account_user_meta(
        account_id=account_id,
        registered_at="2026-01-01 00:00:00",
        message_intensity_level=0,
        companion_primary_type="daily_chat",
        companion_secondary_types=[],
        companion_type_confidence=0.5,
        companion_type_last_evaluated_at="2026-06-20 03:00:00",
        companion_type_source="auto",
        companion_type_expires_at=None,
        companion_type_reasoning="seed",
        safety_risk_trigger_count_30d=0,
        last_evaluated_at="2026-06-20 03:00:00",
    )


def _run_once(now=datetime(2026, 6, 22, 3, 0, 0)):
    from app.products.zhaoxi.jobs.user_meta.scheduler import UserMetaScheduler

    return asyncio.run(
        UserMetaScheduler(page_size=10, inter_account_sleep=0.0).run_once(now=now)
    )


def _daily_relationship(account_id: str, snapshot_date: str = "2026-06-22"):
    from app.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT relationship_stage, agent_need_trust_status, agent_need_growth_status "
            "FROM account_user_meta_daily WHERE account_id = ? AND snapshot_date = ?",
            (account_id, snapshot_date),
        ).fetchone()
    return dict(row) if row else None


def test_run_once_relationship_llm_success(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-run-llm-ok")
    _seed_inbound(account_id="acc-run-llm-ok", session_id=session_id, count=31, date="2026-06-10")
    _seed_companion_recent("acc-run-llm-ok")

    with patch("app.products.zhaoxi.application.relationship.generate_completion", return_value=_rel_json()):
        result = _run_once()

    meta = get_account_user_meta(account_id="acc-run-llm-ok")
    assert meta["relationship_stage"] == "deep_bond"
    assert meta["agent_need_trust_status"] == "stable"
    assert meta["agent_need_growth_status"] == "emerging"
    assert _daily_relationship("acc-run-llm-ok") == {
        "relationship_stage": "deep_bond",
        "agent_need_trust_status": "stable",
        "agent_need_growth_status": "emerging",
    }
    assert result["relationship_evaluated"] == 1
    assert result["relationship_failed"] == 0
    # companion 未受影响
    assert meta["companion_primary_type"] == "daily_chat"


def test_run_once_relationship_llm_failure_keeps_deterministic(fresh_db):
    from app.db import get_account_user_meta

    session_id = _create_account("acc-run-llm-fail")
    _seed_inbound(account_id="acc-run-llm-fail", session_id=session_id, count=31, date="2026-06-10")
    _seed_companion_recent("acc-run-llm-fail")

    with patch("app.products.zhaoxi.application.relationship.generate_completion", side_effect=RuntimeError("down")):
        result = _run_once()

    meta = get_account_user_meta(account_id="acc-run-llm-fail")
    # 保留 B4 确定性结果
    assert meta["relationship_stage"] == "acquainted"
    assert _daily_relationship("acc-run-llm-fail")["relationship_stage"] == "acquainted"
    assert result["relationship_failed"] == 1
    assert any(e.get("step") == "relationship_evaluation" for e in result["errors"])
    # companion 不受影响
    assert meta["companion_primary_type"] == "daily_chat"


def test_run_once_relationship_llm_skipped_low_signal(fresh_db):
    session_id = _create_account("acc-run-llm-skip")
    _seed_inbound(account_id="acc-run-llm-skip", session_id=session_id, count=1, date="2026-06-10")

    with patch("app.products.zhaoxi.application.relationship.generate_completion", return_value=_rel_json()) as mock_llm:
        result = _run_once()

    assert result["relationship_evaluated"] == 0
    mock_llm.assert_not_called()
