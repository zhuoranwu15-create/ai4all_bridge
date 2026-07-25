"""Seam A：微信 adapter（build_channel_input_from_openclaw）+ 渠道无关入口 run_turn_for_account。

核心不变量（原则一）：handle_openclaw_turn 现为薄封装——经微信 adapter 把 OpenClaw DTO 归一为
规范化 ChannelTurnInput 后委派 run_turn_for_account；入口守卫（非私聊/unbound）在 adapter 收口，
不进核心。核心链路（run_turn_for_account 及四阶段）从此不再触碰任何渠道 DTO。
"""
from unittest.mock import patch

import app.db as db
import app.turn_service as turn_service
from app.schemas import OpenClawTurnRequest
from app.bootstrap.product_registry import build_test_product_registry
from app.turn_service import (
    ChannelTurnInput,
    build_channel_input_from_openclaw,
    run_turn_for_account,
)


def _payload(chat_type: str = "private") -> OpenClawTurnRequest:
    return OpenClawTurnRequest(
        channel="openclaw-weixin",
        channel_account_id="chan-seamA",
        account_id="chan-seamA",
        session_key="session-seamA",
        sender_id="sender-seamA",
        chat_id="sender-seamA",
        chat_type=chat_type,
        message_type="text",
        message_id="seamA-1",
        text="你好",
    )


def test_adapter_normalizes_openclaw_payload():
    """绑定命中时，微信 adapter 把 OpenClaw payload 归一为规范化 ChannelTurnInput。

    身份/账号/cap 均已由 adapter 解析并落到 ctx；消息内容字段来自 payload、运行开关透传。
    """
    payload = _payload()
    with patch.object(
        turn_service,
        "resolve_account_id_for_inbound_channel_identity",
        return_value="acct-seamA",
    ):
        ctx = build_channel_input_from_openclaw(
            payload, background_loop=None, force_web_search_enabled=True
        )

    assert isinstance(ctx, ChannelTurnInput)
    assert ctx.account_id == "acct-seamA"
    assert ctx.app_id == "zhaoxi"
    assert ctx.cap is turn_service.get_channel_capability("openclaw-weixin")
    assert ctx.identity.channel == "openclaw-weixin"
    assert ctx.message_id == "seamA-1"
    assert ctx.message_type == "text"
    assert ctx.text == "你好"
    assert ctx.force_web_search_enabled is True


def test_handle_openclaw_turn_delegates_to_run_turn_for_account():
    """薄封装：adapter 归一后委派 run_turn_for_account，账号/运行开关透传。"""
    payload = _payload()
    sentinel_loop = object()
    captured = {}

    def _fake_run(ctx):
        captured["ctx"] = ctx
        return turn_service.OpenClawTurnResponse(status="ok", reply="ok")

    with patch.object(
        turn_service,
        "resolve_account_id_for_inbound_channel_identity",
        return_value="acct-seamA",
    ), patch.object(turn_service, "run_turn_for_account", _fake_run):
        turn_service.handle_openclaw_turn(
            payload,
            background_loop=sentinel_loop,
            force_web_search_enabled=True,
        )

    ctx = captured["ctx"]
    assert isinstance(ctx, ChannelTurnInput)
    assert ctx.account_id == "acct-seamA"
    assert ctx.background_loop is sentinel_loop
    assert ctx.force_web_search_enabled is True


def test_handle_openclaw_turn_unbound_ignored_without_entering_core():
    """未命中 binding 且要求绑定：adapter 入口收口为 ignored，run_turn_for_account 零调用。"""
    called = {"run": False}

    def _fake_run(ctx):
        called["run"] = True
        return turn_service.OpenClawTurnResponse(status="ok")

    with patch.object(
        turn_service,
        "resolve_account_id_for_inbound_channel_identity",
        return_value=None,
    ), patch.object(
        turn_service.settings, "openclaw_inbound_require_binding", True
    ), patch.object(turn_service, "run_turn_for_account", _fake_run):
        res = turn_service.handle_openclaw_turn(_payload())

    assert res.status == "ignored"
    assert res.no_reply is True
    assert called["run"] is False


def test_non_private_ignored_at_adapter():
    """非私聊在 adapter 入口即被忽略（不解析账号、不落 DB、不调 LLM）——守卫等价历史。"""
    res = turn_service.handle_openclaw_turn(_payload(chat_type="group"))
    assert res.status == "ignored"
    assert res.no_reply is True


def test_disabled_membership_is_rejected_before_turn_side_effects(fresh_db, caplog):
    user = db.create_or_get_platform_user_by_phone(phone="13800037901")
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="停服账号"
    )["account"]
    db.update_product_membership_status(
        platform_user_id=user["id"], app_id="zhaoxi", status="disabled"
    )

    with patch.object(
        turn_service,
        "resolve_account_id_for_inbound_channel_identity",
        return_value=account["id"],
    ), patch.object(
        turn_service,
        "_prepare_turn",
        side_effect=AssertionError("disabled membership must not enter turn setup"),
    ):
        response = turn_service.handle_openclaw_turn(_payload())

    assert response.status == "disabled"
    assert response.no_reply is True
    assert response.metadata["reason"] == "product_membership_disabled"
    assert response.metadata["app_id"] == "zhaoxi"
    assert "product membership rejected" in caplog.text


def test_product_scope_mismatch_is_rejected_before_turn_side_effects(fresh_db, caplog):
    """规范化入口声明的产品与账号产品不一致时，不能创建 session/binding/message。"""

    registry = build_test_product_registry()
    user = db.create_or_get_platform_user_by_phone(phone="13800037902")
    db.ensure_product_membership(
        platform_user_id=user["id"], app_id="test_product", registry=registry
    )
    account = db.create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="测试产品账号",
        app_id="test_product",
        registry=registry,
    )["account"]
    with db.connect() as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("sessions", "channel_bindings", "messages")
        }

    with patch.object(
        turn_service,
        "resolve_account_id_for_inbound_channel_identity",
        return_value=account["id"],
    ), patch.object(
        turn_service,
        "_prepare_turn",
        side_effect=AssertionError("scope mismatch must not enter turn setup"),
    ):
        response = turn_service.handle_openclaw_turn(_payload())

    with db.connect() as conn:
        after = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("sessions", "channel_bindings", "messages")
        }

    assert response.status == "disabled"
    assert response.no_reply is True
    assert response.metadata["reason"] == "product_scope_mismatch"
    assert response.metadata["app_id"] == "zhaoxi"
    assert after == before
    assert "product scope rejected" in caplog.text
