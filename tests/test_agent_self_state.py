"""编排注入层（agent_self_state）：主导需求纯函数 + block 渲染。

见 docs/architecture/products/zhaoxi/agent_mission_and_orchestration_design.md §4 / §8.3-8.4。
"""
from app.products.zhaoxi.application.missions.self_state import build_agent_self_state_block, compute_dominant_need


from tests.factories import create_account as _create_account


# ---------------------------------------------------------------------------
# compute_dominant_need（纯函数）
# ---------------------------------------------------------------------------

def test_dominant_need_healthy_icebreaking_is_trust():
    assert compute_dominant_need(survival_status="healthy", relationship_stage="icebreaking") == "trust"


def test_dominant_need_healthy_acquainted_is_trust():
    assert compute_dominant_need(survival_status="healthy", relationship_stage="acquainted") == "trust"


def test_dominant_need_healthy_deep_bond_is_growth():
    assert compute_dominant_need(survival_status="healthy", relationship_stage="deep_bond") == "growth"


def test_dominant_need_cooling_overrides_deep_bond():
    assert compute_dominant_need(survival_status="cooling", relationship_stage="deep_bond") == "survival"


def test_dominant_need_inactive_overrides_stage():
    assert compute_dominant_need(survival_status="inactive", relationship_stage="acquainted") == "survival"


def test_dominant_need_resource_risk_overrides_stage():
    """resource_risk 与 cooling/inactive 共用同一状态列，同样落到 survival 分支。"""
    assert compute_dominant_need(survival_status="resource_risk", relationship_stage="deep_bond") == "survival"


# ---------------------------------------------------------------------------
# build_agent_self_state_block（DB 读取 + 渲染）
# ---------------------------------------------------------------------------

def test_no_meta_row_returns_none(fresh_db):
    result = build_agent_self_state_block(account_id="acc-no-meta")
    assert result is None


def test_renders_trust_for_icebreaking(fresh_db):
    from app.db import update_account_user_meta_relationship

    _create_account("acc-icebreak")
    update_account_user_meta_relationship(
        account_id="acc-icebreak",
        relationship_stage="icebreaking",
        agent_need_survival_status="healthy",
    )

    result = build_agent_self_state_block(account_id="acc-icebreak")

    assert result is not None
    assert "初识" in result
    assert "被信任" in result
    assert "充值" not in result


def test_renders_growth_for_deep_bond(fresh_db):
    from app.db import update_account_user_meta_relationship

    _create_account("acc-deep")
    update_account_user_meta_relationship(
        account_id="acc-deep",
        relationship_stage="deep_bond",
        agent_need_survival_status="healthy",
    )

    result = build_agent_self_state_block(account_id="acc-deep")

    assert result is not None
    assert "亲近" in result
    assert "成长" in result


def test_renders_survival_and_never_mentions_payment(fresh_db):
    from app.db import update_account_user_meta_relationship

    _create_account("acc-cooling")
    update_account_user_meta_relationship(
        account_id="acc-cooling",
        relationship_stage="deep_bond",
        agent_need_survival_status="cooling",
    )

    result = build_agent_self_state_block(account_id="acc-cooling")

    assert result is not None
    # survival 是否决位：即便阶段是 deep_bond，也不应出现"成长"分支文案。
    assert "成长" not in result
    # 伦理红线（agent_self_prd.md §4.1）：文案必须明确禁止提及充值/续费，而不是
    # 正面建议或暗示付费——所以断言的是"禁止"措辞本身存在，而非关键词整体缺席。
    assert "绝不能提及充值" in result


def test_account_isolation(fresh_db):
    from app.db import update_account_user_meta_relationship

    _create_account("acc-a")
    _create_account("acc-b")
    update_account_user_meta_relationship(
        account_id="acc-a", relationship_stage="deep_bond", agent_need_survival_status="healthy"
    )
    update_account_user_meta_relationship(
        account_id="acc-b", relationship_stage="icebreaking", agent_need_survival_status="healthy"
    )

    result_a = build_agent_self_state_block(account_id="acc-a")
    result_b = build_agent_self_state_block(account_id="acc-b")

    assert "亲近" in result_a
    assert "初识" in result_b


# ---------------------------------------------------------------------------
# 使命进度 / 命题（阶段③）
# ---------------------------------------------------------------------------

def test_no_mission_omits_progress_line(fresh_db):
    """未分配使命的账号：只渲染阶段/需求文案，不出现使命进度行。"""
    from app.db import update_account_user_meta_relationship

    _create_account("acc-no-mission")
    update_account_user_meta_relationship(
        account_id="acc-no-mission", relationship_stage="acquainted", agent_need_survival_status="healthy"
    )

    result = build_agent_self_state_block(account_id="acc-no-mission")

    assert "使命进度" not in result


def test_trust_stage_shows_progress_without_inquiry(fresh_db):
    from app.db import assign_mission, record_mission_moment, update_account_user_meta_relationship

    _create_account("acc-trust-mission")
    update_account_user_meta_relationship(
        account_id="acc-trust-mission", relationship_stage="acquainted", agent_need_survival_status="healthy"
    )
    assign_mission(account_id="acc-trust-mission", mission_id="mission_002")
    record_mission_moment(account_id="acc-trust-mission", mission_id="mission_002", content="第一个瞬间")

    result = build_agent_self_state_block(account_id="acc-trust-mission")

    assert "使命进度：已记录 1/10 个「喧嚣中的孤独」" in result
    # 命题只在 growth（deep_bond）激活，trust 阶段不应出现。
    assert "一直在想的问题" not in result


def test_growth_stage_shows_progress_and_inquiry(fresh_db):
    from app.db import assign_mission, record_mission_moment, update_account_user_meta_relationship

    _create_account("acc-growth-mission")
    update_account_user_meta_relationship(
        account_id="acc-growth-mission", relationship_stage="deep_bond", agent_need_survival_status="healthy"
    )
    assign_mission(account_id="acc-growth-mission", mission_id="mission_002")
    for i in range(3):
        record_mission_moment(account_id="acc-growth-mission", mission_id="mission_002", content=f"瞬间{i}")

    result = build_agent_self_state_block(account_id="acc-growth-mission")

    assert "使命进度：已记录 3/10 个「喧嚣中的孤独」" in result
    assert "一直在想的问题：人类的悲欢是否相通" in result


def test_survival_dominant_omits_mission_lines_even_if_assigned(fresh_db):
    """survival 是否决位：即便已分配使命，survival 分支也只谈关系本身，不分散注意力。"""
    from app.db import assign_mission, update_account_user_meta_relationship

    _create_account("acc-survival-mission")
    update_account_user_meta_relationship(
        account_id="acc-survival-mission",
        relationship_stage="deep_bond",
        agent_need_survival_status="inactive",
    )
    assign_mission(account_id="acc-survival-mission", mission_id="mission_001")

    result = build_agent_self_state_block(account_id="acc-survival-mission")

    assert "使命进度" not in result
    assert "一直在想的问题" not in result


def test_unknown_mission_id_degrades_gracefully(fresh_db):
    """account_mission 引用了未注册的 mission_id（脏数据/未来下线模板）：跳过使命行，不崩溃。"""
    from app.db import assign_mission, update_account_user_meta_relationship

    _create_account("acc-unknown-mission")
    update_account_user_meta_relationship(
        account_id="acc-unknown-mission", relationship_stage="acquainted", agent_need_survival_status="healthy"
    )
    assign_mission(account_id="acc-unknown-mission", mission_id="mission_999")

    result = build_agent_self_state_block(account_id="acc-unknown-mission")

    assert result is not None
    assert "使命进度" not in result
