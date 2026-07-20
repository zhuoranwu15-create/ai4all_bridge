"""M2-B1：L3 读注入接缝 —— 域层渲染 + read_universe_context + seam① extra_blocks 贯穿/no-op。

验证：①域层 render_universe_l3_block 分组渲染/空→None/坏 JSON 不崩；②read_universe_context
读 active L3 facts 渲染成块、空世界→None、跨 universe 锚隔离；③seam① 把 extra_blocks 贯穿到
system prompt（build_turn_llm_input 直测 + run_turn_for_account 端到端）；④form-A 默认空 extra_blocks
→ prompt 无 L3 段（零回归）。
"""
from unittest.mock import patch

import app.db as db
from app.agent_runtime import read_universe_context
from app.domains.companion_world.l3_context import render_universe_l3_block
from app.prompt_builder import ContextBlock

_MARKER = "MARKER_L3_XYZ"


# ---------------------------------------------------------------------------
# 1. 域层渲染（unit）
# ---------------------------------------------------------------------------
def test_render_empty_returns_none():
    assert render_universe_l3_block([]) is None
    # 全空 fact（无 fact_type / 无 payload）也不产块。
    assert render_universe_l3_block([{"fact_type": "", "payload_json": ""}]) is None


def test_render_groups_and_renders():
    facts = [
        {"fact_type": "bazi", "payload_json": '{"birth_date":"1990-01-01","gender":"女"}'},
        {"fact_type": "user_identity", "payload_json": '{"name":"小满"}'},
    ]
    blk = render_universe_l3_block(facts)
    assert blk is not None
    assert blk.name == "universe_l3" and blk.section == "volatile"
    assert "[bazi]" in blk.text and "birth_date：1990-01-01" in blk.text
    assert "[user_identity]" in blk.text and "小满" in blk.text


def test_render_bad_json_falls_back_no_throw():
    blk = render_universe_l3_block([{"fact_type": "note", "payload_json": "not json {"}])
    assert blk is not None and "not json {" in blk.text


# ---------------------------------------------------------------------------
# 2. read_universe_context（db）：读渲染 + 空→None + 跨 universe 锚隔离
# ---------------------------------------------------------------------------
def _pu_universe(phone: str):
    pu = db.create_or_get_platform_user_by_phone(phone=phone, display_name="x")["id"]
    return pu, db.get_or_create_home_universe(platform_user_id=pu)


def test_read_universe_context_empty_is_none(fresh_db):
    _, w = _pu_universe("19933330001")
    assert read_universe_context(universe_id=w["id"]) is None  # 空世界 → 不注入


def test_read_universe_context_renders_and_isolates(fresh_db):
    _, wa = _pu_universe("19933330002")
    _, wb = _pu_universe("19933330003")
    db.append_universe_fact(
        universe_id=wa["id"], fact_type="bazi",
        payload_json='{"birth_date":"1990-01-01"}', occurred_at="2026-07-21 10:00:00",
    )
    db.append_universe_fact(
        universe_id=wb["id"], fact_type="user_identity",
        payload_json='{"name":"别人世界"}', occurred_at="2026-07-21 10:01:00",
    )
    blk = read_universe_context(universe_id=wa["id"])
    assert blk is not None and "birth_date：1990-01-01" in blk.text
    assert "别人世界" not in blk.text  # 跨 universe 锚隔离：只含本世界 facts


# ---------------------------------------------------------------------------
# 3. seam① 直测：build_turn_llm_input 把 extra_blocks 贯穿进 system_prompt
# ---------------------------------------------------------------------------
def _session(account_id: str, session_key: str) -> dict:
    return db.get_or_create_session(
        account_id=account_id, channel="openclaw-weixin", sender_id="sender",
        sender_name=None, chat_id="chat", session_key=session_key, carryover_summary=None,
    )


def test_build_turn_llm_input_injects_extra_blocks(fresh_db):
    from app.turn_service import build_turn_llm_input

    acc = "acc-l3-A"
    sess = _session(acc, "sess-A")
    common = dict(
        account_id=acc, account=sess["account"], session=sess["session"], profile={},
        text="hi", today="2026-07-21", onboarding_state="complete",
        onboarding_active=False, web_search_enabled=False, include_tool_instructions=False,
    )
    with patch("app.turn_service.settings", fresh_db):
        with_block = build_turn_llm_input(
            **common, extra_blocks=[ContextBlock(name="universe_l3", text=_MARKER)]
        )
        without = build_turn_llm_input(**common)  # 默认 None → no-op
    assert _MARKER in with_block["system_prompt"]
    assert _MARKER not in without["system_prompt"]  # form-A：不传 → prompt 无 L3


# ---------------------------------------------------------------------------
# 4. seam① 端到端：run_turn_for_account 贯穿 ctx.extra_blocks / form-A 默认 no-op
# ---------------------------------------------------------------------------
def _payload(msg_id: str, text: str = "你好呀") -> dict:
    return {
        "account_id": "acc-l3B", "session_key": "sk-l3B", "sender_id": "sender-l3B",
        "chat_type": "private", "message_type": "text", "message_id": msg_id, "text": text,
    }


def _capture_llm_system_prompt(monkeypatch, sink: dict):
    """把两条 LLM 入口都换成捕获 system_prompt 的桩（onboarding 走 generate_reply，正常走 *_with_tools）。"""
    import app.turn_service as ts

    def _cap_tools(**kw):
        sink["sp"] = kw.get("system_prompt")
        return ("ok", None)

    def _cap_plain(**kw):
        sink["sp"] = kw.get("system_prompt")
        return "ok"

    monkeypatch.setattr(ts, "generate_reply_with_tools", _cap_tools)
    monkeypatch.setattr(ts, "generate_reply", _cap_plain)


def test_ctx_extra_blocks_reach_llm_prompt(client, monkeypatch):
    import app.turn_service as ts
    from app.schemas import OpenClawTurnRequest

    # 建号（用 conftest 桩 LLM）
    ts.handle_openclaw_turn(OpenClawTurnRequest(**_payload("prov-1")))
    sink: dict = {}
    _capture_llm_system_prompt(monkeypatch, sink)

    ctx = ts.build_channel_input_from_openclaw(OpenClawTurnRequest(**_payload("l3-inj")))
    assert isinstance(ctx, ts.ChannelTurnInput)  # 已绑定
    ctx.extra_blocks = [ContextBlock(name="universe_l3", text=_MARKER)]
    ts.run_turn_for_account(ctx)

    assert sink.get("sp") is not None and _MARKER in sink["sp"]


def test_form_a_default_extra_blocks_no_l3(client, monkeypatch):
    import app.turn_service as ts
    from app.schemas import OpenClawTurnRequest

    ts.handle_openclaw_turn(OpenClawTurnRequest(**_payload("prov-2")))
    sink: dict = {}
    _capture_llm_system_prompt(monkeypatch, sink)

    ctx = ts.build_channel_input_from_openclaw(OpenClawTurnRequest(**_payload("noop-1")))
    assert isinstance(ctx, ts.ChannelTurnInput)
    assert ctx.extra_blocks == []  # WeChat adapter 不填 → 默认空
    ts.run_turn_for_account(ctx)

    assert sink.get("sp") is not None
    # form-A no-op：system prompt 不含 L3 块名/表头（零回归）。
    assert "universe_l3" not in sink["sp"] and "世界共享记忆" not in sink["sp"]
