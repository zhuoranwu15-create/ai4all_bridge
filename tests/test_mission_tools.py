"""mission_status / record_mission_moment 工具（agent_mission_and_orchestration_design.md §6/§8.6）。"""
from unittest.mock import MagicMock

from app.tools.executor import execute_tool_call
from app.tools.mission_handlers import handle_mission_status, handle_record_mission_moment
from app.tools.registry import get_default_tools, get_spec
from app.turn_context import TurnContext


def _make_ctx(account_id="acc-tool", session_id=1, message_id="msg-1"):
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id=account_id,
        account={"id": account_id},
        session={"id": session_id},
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id=message_id,
        text="测试",
        today="2026-07-04",
        business_day="2026-07-04",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


from tests.factories import create_account as _create_account


# ---------------------------------------------------------------------------
# Registry gating（has_mission）
# ---------------------------------------------------------------------------

def test_default_tools_exclude_mission_tools_without_flag():
    names = {t["function"]["name"] for t in get_default_tools()}
    assert "mission_status" not in names
    assert "record_mission_moment" not in names


def test_default_tools_include_mission_tools_with_flag():
    names = {t["function"]["name"] for t in get_default_tools(has_mission=True)}
    assert "mission_status" in names
    assert "record_mission_moment" in names


def test_registry_specs_resolvable():
    for name in ("mission_status", "record_mission_moment"):
        spec = get_spec(name)
        assert spec is not None
        assert spec.default_when_flag == "has_mission"


# ---------------------------------------------------------------------------
# mission_status（读）
# ---------------------------------------------------------------------------

def test_mission_status_no_mission(fresh_db):
    _create_account("acc-no-mission")
    ctx = _make_ctx(account_id="acc-no-mission")

    result = handle_mission_status({}, ctx)

    assert result == {"status": "ok", "has_mission": False}


def test_mission_status_with_mission_and_progress(fresh_db):
    from app.db import assign_mission, record_mission_moment

    _create_account("acc-status")
    assign_mission(account_id="acc-status", mission_id="mission_002")
    record_mission_moment(account_id="acc-status", mission_id="mission_002", content="第一个瞬间")
    ctx = _make_ctx(account_id="acc-status")

    result = handle_mission_status({}, ctx)

    assert result["has_mission"] is True
    assert result["mission_id"] == "mission_002"
    assert result["progress"] == 1
    assert result["target_count"] == 10
    assert result["remaining"] == 9
    assert result["recent_moments"][0]["content"] == "第一个瞬间"


def test_mission_status_account_isolation(fresh_db):
    from app.db import assign_mission

    _create_account("acc-x")
    _create_account("acc-y")
    assign_mission(account_id="acc-x", mission_id="mission_001")

    result_x = handle_mission_status({}, _make_ctx(account_id="acc-x"))
    result_y = handle_mission_status({}, _make_ctx(account_id="acc-y"))

    assert result_x["has_mission"] is True
    assert result_y["has_mission"] is False


# ---------------------------------------------------------------------------
# record_mission_moment（写）
# ---------------------------------------------------------------------------

def test_record_moment_success(fresh_db):
    from app.db import assign_mission

    _create_account("acc-record")
    assign_mission(account_id="acc-record", mission_id="mission_002")
    ctx = _make_ctx(account_id="acc-record")

    result = handle_record_mission_moment({"content": "地铁上和陌生人相视一笑"}, ctx)

    assert result["status"] == "recorded"
    assert result["progress"] == 1
    assert result["remaining"] == 9
    assert "moment_id" in result


def test_record_moment_no_mission_returns_error(fresh_db):
    _create_account("acc-no-mission-record")
    ctx = _make_ctx(account_id="acc-no-mission-record")

    result = handle_record_mission_moment({"content": "一些内容"}, ctx)

    assert result == {"error": "尚未分配使命"}


def test_record_moment_empty_content_returns_error(fresh_db):
    from app.db import assign_mission

    _create_account("acc-empty")
    assign_mission(account_id="acc-empty", mission_id="mission_001")
    ctx = _make_ctx(account_id="acc-empty")

    result = handle_record_mission_moment({"content": "   "}, ctx)

    assert result == {"error": "content 不能为空"}


def test_record_moment_already_complete_does_not_insert(fresh_db):
    from app.db import assign_mission, count_mission_moments, record_mission_moment

    _create_account("acc-complete")
    assign_mission(account_id="acc-complete", mission_id="mission_002")
    for i in range(10):
        record_mission_moment(account_id="acc-complete", mission_id="mission_002", content=f"瞬间{i}")
    ctx = _make_ctx(account_id="acc-complete")

    result = handle_record_mission_moment({"content": "第十一个"}, ctx)

    assert result["status"] == "already_complete"
    assert count_mission_moments(account_id="acc-complete", mission_id="mission_002") == 10


def test_record_moment_ignores_forged_account_id(fresh_db):
    """account_id 一律取 ctx，忽略 args 里的同名字段（镜像 proactive settings 的账号隔离测试）。"""
    from app.db import assign_mission, count_mission_moments

    _create_account("acc-real")
    _create_account("acc-victim")
    assign_mission(account_id="acc-real", mission_id="mission_001")
    assign_mission(account_id="acc-victim", mission_id="mission_001")
    ctx = _make_ctx(account_id="acc-real")

    handle_record_mission_moment({"content": "真实的瞬间", "account_id": "acc-victim"}, ctx)

    assert count_mission_moments(account_id="acc-real", mission_id="mission_001") == 1
    assert count_mission_moments(account_id="acc-victim", mission_id="mission_001") == 0


def test_record_moment_via_executor_with_invocation_call_style(fresh_db):
    """通过 execute_tool_call 走完整分发路径，验证 CALL_INVOCATION 调用约定不报错。"""
    from app.db import assign_mission

    _create_account("acc-executor")
    assign_mission(account_id="acc-executor", mission_id="mission_001")
    ctx = _make_ctx(account_id="acc-executor")

    result = execute_tool_call(
        "record_mission_moment", {"content": "通过 executor 记录"}, ctx, tool_invocation_id=123
    )

    assert result["status"] == "recorded"
