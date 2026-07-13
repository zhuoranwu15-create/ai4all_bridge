"""Seam A：ChannelTurnInput + 渠道无关入口 run_turn_for_account 守卫。

核心不变量（原则一）：handle_openclaw_turn 现为薄封装，仅构造 ChannelTurnInput 后委派
run_turn_for_account——委派透明、行为等价历史；run_turn_for_account 为 Phase 1 /web/turn
预留的复用入口。
"""
from unittest.mock import patch

import app.turn_service as turn_service
from app.schemas import OpenClawTurnRequest
from app.turn_service import ChannelTurnInput, run_turn_for_account


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


def test_handle_openclaw_turn_delegates_to_run_turn_for_account():
    """薄封装：handle_openclaw_turn 把 payload + 运行开关原样包进 ChannelTurnInput 后委派。"""
    payload = _payload()
    sentinel_loop = object()
    captured = {}

    def _fake_run(ctx):
        captured["ctx"] = ctx
        return turn_service.OpenClawTurnResponse(status="ok", reply="ok")

    with patch.object(turn_service, "run_turn_for_account", _fake_run):
        turn_service.handle_openclaw_turn(
            payload,
            background_loop=sentinel_loop,
            force_web_search_enabled=True,
        )

    ctx = captured["ctx"]
    assert isinstance(ctx, ChannelTurnInput)
    assert ctx.payload is payload
    assert ctx.background_loop is sentinel_loop
    assert ctx.force_web_search_enabled is True


def test_run_turn_for_account_non_private_ignored():
    """非私聊在入口即被忽略（不落 DB、不调 LLM）——与历史 handle_openclaw_turn 同一守卫。"""
    res = run_turn_for_account(ChannelTurnInput(payload=_payload(chat_type="group")))
    assert res.status == "ignored"
    assert res.no_reply is True
