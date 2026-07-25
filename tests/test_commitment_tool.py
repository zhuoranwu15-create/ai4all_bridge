"""create_commitment 工具：把隐藏 commitment 抽取改成工具调用后的确定性校验。

对照 extract_commitment_from_turn 原有的账号/proactive/route 前置检查——工具被
调用 != 跳过这些硬约束，只是"值不值得记"这个语义判断现在由主模型决定要不要调用
工具来体现，服务端不再重复判断。
"""
from datetime import timedelta
from unittest.mock import MagicMock

from app.time_utils import beijing_naive_now
from app.products.zhaoxi.tools.commitment_handlers import handle_create_commitment
from app.tools.registry import get_default_tools
from app.agent_runtime.context.models import TurnContext


def _make_ctx(account_id="acc-com-tool", session_id=1, message_id="msg-1"):
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id=account_id,
        app_id="zhaoxi",
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


from tests.factories import create_route as _create_route


def _create_state(account_id: str, *, enabled: bool = True) -> None:
    from app.products.zhaoxi.proactive.store.account_state import ensure_account_state

    ensure_account_state(
        account_id=account_id,
        enabled=enabled,
        next_scan_at=beijing_naive_now(),
    )


def _future(days=1, hours=0):
    return (beijing_naive_now() + timedelta(days=days, hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_default_tools_always_include_create_commitment():
    names = {t["function"]["name"] for t in get_default_tools()}
    assert "create_commitment" in names


def test_handle_create_commitment_empty_text_rejected(fresh_db):
    ctx = _make_ctx(account_id="acc-com-empty")
    result = handle_create_commitment({"text": "  ", "due_at": _future()}, ctx)
    assert "error" in result


def test_handle_create_commitment_account_not_found(fresh_db):
    ctx = _make_ctx(account_id="acc-com-missing")
    result = handle_create_commitment({"text": "记一下", "due_at": _future()}, ctx)
    assert "error" in result


def test_handle_create_commitment_account_inactive(fresh_db):
    from app.db import set_account_status

    account_id = "acc-com-inactive"
    _create_account(account_id)
    set_account_status(account_id=account_id, status="disabled")
    ctx = _make_ctx(account_id=account_id)

    result = handle_create_commitment({"text": "记一下", "due_at": _future()}, ctx)
    assert "error" in result


def test_handle_create_commitment_proactive_disabled_skips(fresh_db):
    account_id = "acc-com-proactive-off"
    _create_account(account_id)
    _create_route(account_id)
    _create_state(account_id, enabled=False)
    ctx = _make_ctx(account_id=account_id)

    result = handle_create_commitment({"text": "记一下", "due_at": _future()}, ctx)
    assert result == {"status": "skipped", "reason": "proactive_disabled"}


def test_handle_create_commitment_missing_route_skips(fresh_db):
    account_id = "acc-com-no-route"
    _create_account(account_id)
    _create_state(account_id, enabled=True)
    ctx = _make_ctx(account_id=account_id)

    result = handle_create_commitment({"text": "记一下", "due_at": _future()}, ctx)
    assert result == {"status": "skipped", "reason": "missing_channel_route"}


def test_handle_create_commitment_invalid_due_at_format(fresh_db):
    account_id = "acc-com-bad-format"
    _create_account(account_id)
    _create_route(account_id)
    _create_state(account_id)
    ctx = _make_ctx(account_id=account_id)

    result = handle_create_commitment({"text": "记一下", "due_at": "not-a-date"}, ctx)
    assert "error" in result


def test_handle_create_commitment_due_at_in_past_rejected(fresh_db):
    account_id = "acc-com-past"
    _create_account(account_id)
    _create_route(account_id)
    _create_state(account_id)
    ctx = _make_ctx(account_id=account_id)

    past = (beijing_naive_now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    result = handle_create_commitment({"text": "记一下", "due_at": past}, ctx)
    assert "error" in result


def test_handle_create_commitment_due_at_exceeds_max_days_rejected(fresh_db):
    account_id = "acc-com-toofar"
    _create_account(account_id)
    _create_route(account_id)
    _create_state(account_id)
    ctx = _make_ctx(account_id=account_id)

    result = handle_create_commitment(
        {"text": "记一下", "due_at": _future(days=30)}, ctx
    )
    assert "error" in result


def test_handle_create_commitment_success(fresh_db):
    from app.db import list_proactive_commitments_for_account

    account_id = "acc-com-success"
    _create_account(account_id)
    _create_route(account_id)
    _create_state(account_id)
    ctx = _make_ctx(account_id=account_id, message_id="msg-success-1")

    result = handle_create_commitment(
        {"text": "下周面试记得问问结果", "due_at": _future(days=2), "reason": "用户提到面试"},
        ctx,
    )

    assert result["status"] == "created"
    commitments = list_proactive_commitments_for_account(account_id=account_id)
    assert len(commitments) == 1
    assert commitments[0]["text"] == "下周面试记得问问结果"
    assert commitments[0]["reason"] == "用户提到面试"
    assert commitments[0]["metadata"]["source"] == "tool_use"
    assert commitments[0]["dedupe_key"] == f"commitment:tool:{account_id}:msg-success-1"
