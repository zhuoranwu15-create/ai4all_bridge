"""app.products.zhaoxi.infrastructure.persistence.mission：使命分配 + 记录的瞬间（agent_mission_and_orchestration_design.md §3/§8.1）。"""
import pytest

from app.products.zhaoxi.infrastructure.persistence.mission import (
    assign_mission,
    count_mission_moments,
    get_account_mission,
    list_mission_moments,
    record_mission_moment,
)


from tests.factories import create_account as _create_account


# ---------------------------------------------------------------------------
# assign_mission / get_account_mission
# ---------------------------------------------------------------------------

def test_assign_mission_then_get(fresh_db):
    _create_account("acc-assign")
    assign_mission(account_id="acc-assign", mission_id="mission_001")

    result = get_account_mission(account_id="acc-assign")

    assert result is not None
    assert result["mission_id"] == "mission_001"
    assert result["assigned_at"]


def test_get_account_mission_none_when_unassigned(fresh_db):
    _create_account("acc-unassigned")
    assert get_account_mission(account_id="acc-unassigned") is None


def test_assign_mission_second_call_does_not_overwrite(fresh_db):
    """不可更改性：DB 层用 ON CONFLICT DO NOTHING 兜底，第二次调用不覆盖已有分配。"""
    _create_account("acc-immutable")
    assign_mission(account_id="acc-immutable", mission_id="mission_001")
    assign_mission(account_id="acc-immutable", mission_id="mission_002")

    result = get_account_mission(account_id="acc-immutable")
    assert result["mission_id"] == "mission_001"


def test_assign_mission_unknown_account_raises(fresh_db):
    with pytest.raises(ValueError):
        assign_mission(account_id="acc-does-not-exist", mission_id="mission_001")


# ---------------------------------------------------------------------------
# record_mission_moment / count / list
# ---------------------------------------------------------------------------

def test_record_and_count_moment(fresh_db):
    _create_account("acc-moment")
    assign_mission(account_id="acc-moment", mission_id="mission_002")

    moment_id = record_mission_moment(
        account_id="acc-moment", mission_id="mission_002", content="地铁上和陌生人相视一笑"
    )

    assert moment_id > 0
    assert count_mission_moments(account_id="acc-moment", mission_id="mission_002") == 1


def test_count_zero_when_no_moments(fresh_db):
    assert count_mission_moments(account_id="acc-empty", mission_id="mission_001") == 0


def test_list_recent_moments_newest_first(fresh_db):
    _create_account("acc-list")
    assign_mission(account_id="acc-list", mission_id="mission_001")
    record_mission_moment(account_id="acc-list", mission_id="mission_001", content="第一个瞬间")
    record_mission_moment(account_id="acc-list", mission_id="mission_001", content="第二个瞬间")

    moments = list_mission_moments(account_id="acc-list", mission_id="mission_001")

    assert [m["content"] for m in moments] == ["第二个瞬间", "第一个瞬间"]


def test_record_moment_empty_content_raises(fresh_db):
    _create_account("acc-empty-content")
    assign_mission(account_id="acc-empty-content", mission_id="mission_001")
    with pytest.raises(ValueError):
        record_mission_moment(account_id="acc-empty-content", mission_id="mission_001", content="   ")


def test_account_isolation_for_moments(fresh_db):
    _create_account("acc-x")
    _create_account("acc-y")
    assign_mission(account_id="acc-x", mission_id="mission_001")
    assign_mission(account_id="acc-y", mission_id="mission_001")
    record_mission_moment(account_id="acc-x", mission_id="mission_001", content="属于 x 的瞬间")

    assert count_mission_moments(account_id="acc-x", mission_id="mission_001") == 1
    assert count_mission_moments(account_id="acc-y", mission_id="mission_001") == 0
