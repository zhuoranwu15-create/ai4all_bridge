"""NookiTurnServices 对通用 Runtime 的最小真实调用契约：人设/工具集/FOCUS_TASK 投影。"""

from __future__ import annotations

from app.agent_runtime.turns.service import ChannelTurnInput
from app.bootstrap.product_registry import NOOKI_APP_ID, PRODUCTION_PRODUCT_REGISTRY
from app.platform.auth.identity import ResolvedIdentity
from app.platform.channels import CHANNEL_APP, CHANNELS
from app.products.nooki.application.turns import run_nooki_turn
from app.products.nooki.domain.goal_breakdown.contracts import PlanDraft
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository
from app.products.nooki.infrastructure.repositories.user_profile import set_explicit_preferences

import app.db as db


def _make_account(phone: str):
    user = db.create_or_get_platform_user_by_phone(phone=phone)
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id=NOOKI_APP_ID, registry=PRODUCTION_PRODUCT_REGISTRY
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="Nooki 测试用户",
        app_id=NOOKI_APP_ID,
        registry=PRODUCTION_PRODUCT_REGISTRY,
    )["account"]
    return user, account


def _run_turn(account_id: str, text: str, monkeypatch, captured: dict):
    def fake_generate_reply_with_tools(**kwargs):
        captured["tool_names"] = {schema["function"]["name"] for schema in kwargs["tools"]}
        captured["context_app_id"] = kwargs["ctx"].app_id
        captured["system_prompt"] = kwargs["system_prompt"]
        return "陪伴回复", None

    monkeypatch.setattr("app.turn_service.generate_reply_with_tools", fake_generate_reply_with_tools)
    monkeypatch.setattr("app.turn_service.record_chat_usage_charge", lambda **_: None)

    identity = ResolvedIdentity(
        ai4all_account_id=account_id,
        session_key=f"app:{account_id}",
        channel=CHANNEL_APP,
        channel_account_id=account_id,
        sender_id=account_id,
        chat_id=None,
    )
    return run_nooki_turn(
        ChannelTurnInput(
            account_id=account_id,
            app_id=NOOKI_APP_ID,
            cap=CHANNELS[CHANNEL_APP],
            identity=identity,
            message_id=f"nooki-msg-{text}",
            event_id=None,
            message_type="text",
            text=text,
            media=None,
            raw={"source": "nooki_turn_contract_test"},
            sender_name="测试用户",
        ),
    )


def test_nooki_turn_exposes_only_goal_breakdown_tools(fresh_db, monkeypatch):
    _, account = _make_account("13800038001")
    captured = {}

    response = _run_turn(account["id"], "hi", monkeypatch, captured)

    assert response.status == "ok"
    assert response.reply == "陪伴回复"
    assert captured["context_app_id"] == NOOKI_APP_ID
    assert captured["tool_names"] == {
        "nooki_create_task_with_options",
        "nooki_select_task_plan",
        "nooki_start_step",
        "nooki_complete_step",
        "nooki_shrink_step",
        "nooki_abandon_task",
        "nooki_list_state",
    }
    assert "用户目前没有进行中的任务" in captured["system_prompt"]


def test_nooki_turn_soul_reflects_archetype_and_companion_name(fresh_db, monkeypatch):
    user, account = _make_account("13800038002")
    set_explicit_preferences(user["id"], archetype="bestie", companion_name="兔兔")
    captured = {}

    _run_turn(account["id"], "hi", monkeypatch, captured)

    assert "元气小兔" in captured["system_prompt"]
    assert "兔兔" in captured["system_prompt"]


def test_nooki_turn_focus_task_block_reflects_active_task(fresh_db, monkeypatch):
    user, account = _make_account("13800038003")
    service = GoalBreakdownService(SqlTaskRepository())
    created = service.create_task_with_options(
        platform_user_id=user["id"],
        title="写周报",
        raw_goal="写周报",
        options=(
            PlanDraft(mode="tiny", title="打开文档", estimated_minutes=2),
            PlanDraft(mode="light", title="列出三个要点", estimated_minutes=6),
            PlanDraft(mode="normal", title="写完第一段", estimated_minutes=20),
        ),
        source_message_id="seed-msg-1",
        operation_id="seed:create:1",
    )
    task = created.task
    plans = created.plans
    selected = next(p for p in plans if p.mode == "tiny")
    service.select_task_plan(
        task.id,
        selected.id,
        platform_user_id=user["id"],
        operation_id="seed:select:1",
    )

    captured = {}
    _run_turn(account["id"], "在写周报", monkeypatch, captured)

    assert "写周报" in captured["system_prompt"]
    assert task.id in captured["system_prompt"]
