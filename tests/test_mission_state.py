"""app.mission_state：使命解析单一事实源（codex review 2026-07-04 修复）。

覆盖"DB 行存在但 mission_id 未注册"这一脏数据/模板下线场景在所有消费方
（tooling 门控 / agent_self_state / mission 工具 / Admin 视图）必须一致降级。
"""
from app.mission_state import build_admin_mission_view, has_resolved_mission, resolve_account_mission


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


def test_resolve_returns_none_when_unassigned(fresh_db):
    _create_account("acc-unassigned")
    assert resolve_account_mission(account_id="acc-unassigned") is None
    assert has_resolved_mission(account_id="acc-unassigned") is False


def test_resolve_returns_template_when_assigned(fresh_db):
    from app.db import assign_mission

    _create_account("acc-assigned")
    assign_mission(account_id="acc-assigned", mission_id="mission_001")

    resolved = resolve_account_mission(account_id="acc-assigned")

    assert resolved is not None
    assert resolved.mission_id == "mission_001"
    assert resolved.template.display_name == "百景"
    assert has_resolved_mission(account_id="acc-assigned") is True


def test_resolve_returns_none_for_unregistered_mission_id(fresh_db):
    """脏数据/模板下线：DB 行存在但 mission_id 未注册，必须和"未分配"表现一致。"""
    from app.db import assign_mission

    _create_account("acc-unregistered")
    assign_mission(account_id="acc-unregistered", mission_id="mission_999")

    assert resolve_account_mission(account_id="acc-unregistered") is None
    assert has_resolved_mission(account_id="acc-unregistered") is False


def test_build_admin_mission_view_none_when_unassigned(fresh_db):
    _create_account("acc-admin-none")
    assert build_admin_mission_view(account_id="acc-admin-none") is None


def test_build_admin_mission_view_includes_progress(fresh_db):
    from app.db import assign_mission, record_mission_moment

    _create_account("acc-admin-progress")
    assign_mission(account_id="acc-admin-progress", mission_id="mission_002")
    record_mission_moment(account_id="acc-admin-progress", mission_id="mission_002", content="第一个瞬间")
    record_mission_moment(account_id="acc-admin-progress", mission_id="mission_002", content="第二个瞬间")

    view = build_admin_mission_view(account_id="acc-admin-progress")

    assert view["mission_id"] == "mission_002"
    assert view["progress"] == 2
    assert view["target_count"] == 10
    assert view["progress_label"] == "2/10"
    assert view["recent_moments"] == ["第二个瞬间", "第一个瞬间"]


def test_build_admin_mission_view_none_for_unregistered_mission_id(fresh_db):
    from app.db import assign_mission

    _create_account("acc-admin-unregistered")
    assign_mission(account_id="acc-admin-unregistered", mission_id="mission_999")

    assert build_admin_mission_view(account_id="acc-admin-unregistered") is None
