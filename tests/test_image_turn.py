"""图片理解 turn 链路测试。

覆盖设计文档 A/B/C 三场景、VL 失败兜底、总开关、账号隔离、memory modality、
以及固定贝壳计费事件。VL 调用全部 mock，不触网；使用内存/临时 SQLite。
"""

from unittest.mock import MagicMock

import app.turn_service as turn_service
from app.schemas import OpenClawTurnRequest


# 不带 channel 字段：identity.channel 不为 openclaw-weixin，从而绕过 onboarding 流程，
# 聚焦测试图片理解链路本身（与 test_turn_rate_limit 等既有 turn 测试一致）。
def _image_payload(
    msg_id,
    *,
    text="",
    url="https://example.com/a.jpg",
    path=None,
    data_base64=None,
    fmt="image",
    sender="sender-img",
    session="acc-img",
):
    return {
        "channel_account_id": "chan-img",
        "account_id": "chan-img",
        "session_key": session,
        "sender_id": sender,
        "chat_id": sender,
        "chat_type": "private",
        "message_type": "image",
        "message_id": msg_id,
        "text": text,
        "media": {"url": url, "path": path, "data_base64": data_base64, "format": fmt},
    }


def _text_payload(msg_id, *, text, sender="sender-img", session="acc-img"):
    return {
        "channel_account_id": "chan-img",
        "account_id": "chan-img",
        "session_key": session,
        "sender_id": sender,
        "chat_id": sender,
        "chat_type": "private",
        "message_type": "text",
        "message_id": msg_id,
        "text": text,
    }


def _setup(monkeypatch, fresh_db, *, describe_return="一只橘猫趴在窗台上，阳光温暖，氛围惬意", capture=None):
    """Patch turn_service for an enabled image-understanding run; return the capture dict."""
    from app.rate_limiter import RateLimiter

    fresh_db.image_understanding_enabled = True
    monkeypatch.setattr(turn_service, "settings", fresh_db)
    monkeypatch.setattr(turn_service, "rate_limiter", RateLimiter())

    cap = capture if capture is not None else {}
    cap.setdefault("reply_calls", [])

    def _describe(**kwargs):
        cap.setdefault("describe_calls", []).append(kwargs)
        return describe_return

    def _reply(**kwargs):
        cap["reply_calls"].append(kwargs)
        return ("这张照片真温馨～它在窗台上做什么呀？", None)

    monkeypatch.setattr(turn_service, "describe_image", _describe)
    monkeypatch.setattr(turn_service, "generate_reply_with_tools", _reply)
    return cap


def _last_user_content(account_id):
    rows = turn_service.list_recent_messages_for_account(account_id=account_id, limit=50)
    users = [r for r in rows if r["role"] == "user"]
    return users[-1]["content"] if users else None


# ---------- A 场景：图 + 文 ----------
def test_image_with_caption_describes_and_replies(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db)
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-A", text="这是哪")))
    assert res.status == "ok"
    assert res.reply == "这张照片真温馨～它在窗台上做什么呀？"
    account_id = res.metadata["account_id"]
    content = _last_user_content(account_id)
    assert "这是哪" in content
    assert "[用户发来一张图片：" in content
    assert "橘猫" in content
    # 主链路被调用，且历史最后一条 user 即合成后的描述。
    assert cap["reply_calls"], "main LLM should be called for image+text"
    assert "橘猫" in cap["reply_calls"][-1]["history"][-1]["content"]
    # caption 透传给 VL。
    assert cap["describe_calls"][-1]["caption"] == "这是哪"


# ---------- B 场景：纯图片 ----------
def test_pure_image_describes_without_caption(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db)
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-B", text="")))
    assert res.status == "ok"
    account_id = res.metadata["account_id"]
    content = _last_user_content(account_id)
    assert content.startswith("[用户发来一张图片：")
    assert cap["reply_calls"], "main LLM should generate persona reply for pure image"


# ---------- C 场景：图后追问靠历史描述 ----------
def test_image_followup_uses_history(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db, describe_return="星巴克门店招牌，绿色 logo，傍晚街景")
    turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-C1", text="")))
    # 下一轮纯文本追问。
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_text_payload("img-C2", text="这什么牌子")))
    assert res.status == "ok"
    history = cap["reply_calls"][-1]["history"]
    joined = "\n".join(m["content"] for m in history)
    assert "星巴克" in joined, "上一轮图片描述应在历史中供追问引用"


# ---------- VL 失败兜底 ----------
def test_vl_failure_falls_back_without_main_llm(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db, describe_return=None)
    charge_spy = MagicMock()
    monkeypatch.setattr(turn_service, "record_image_understanding_charge", charge_spy)
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-fail", text="看看这个")))
    assert res.status == "ok"
    assert res.reply == fresh_db.image_understanding_fallback_text
    assert not cap["reply_calls"], "main LLM must NOT be called when VL has no description"
    charge_spy.assert_not_called()


# ---------- 总开关关闭 ----------
def test_disabled_switch_skips_vl(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db)
    fresh_db.image_understanding_enabled = False
    res = turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-off", text="x")))
    assert res.reply == fresh_db.image_understanding_fallback_text
    assert "describe_calls" not in cap, "VL must not be called when disabled"


# ---------- 账号隔离 ----------
def test_account_isolation(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db, describe_return="A 账号的猫")
    res_a = turn_service.handle_openclaw_turn(
        OpenClawTurnRequest(**_image_payload("iso-A", text="", sender="peer-a", session="acc-iso-a"))
    )
    res_b = turn_service.handle_openclaw_turn(
        OpenClawTurnRequest(**_text_payload("iso-B", text="你好", sender="peer-b", session="acc-iso-b"))
    )
    acct_a = res_a.metadata["account_id"]
    acct_b = res_b.metadata["account_id"]
    assert acct_a != acct_b
    rows_b = turn_service.list_recent_messages_for_account(account_id=acct_b, limit=50)
    assert all("A 账号的猫" not in (r["content"] or "") for r in rows_b)


# ---------- memory modality ----------
def test_memory_write_receives_image_modality(monkeypatch, fresh_db):
    cap = _setup(monkeypatch, fresh_db)
    write_spy = MagicMock(return_value=None)
    monkeypatch.setattr(turn_service, "write_memory", write_spy)
    monkeypatch.setattr(turn_service, "extract_commitment_from_turn", MagicMock())
    loop = MagicMock()
    turn_service.handle_openclaw_turn(
        OpenClawTurnRequest(**_image_payload("img-mem", text="hi")),
        background_loop=loop,
    )
    assert write_spy.called
    assert write_spy.call_args.kwargs["modality"] == "image"


# ---------- 计费：成功记一次独立成本事件 ----------
def test_charge_called_on_success(monkeypatch, fresh_db):
    _setup(monkeypatch, fresh_db)
    charge_spy = MagicMock()
    monkeypatch.setattr(turn_service, "record_image_understanding_charge", charge_spy)
    turn_service.handle_openclaw_turn(OpenClawTurnRequest(**_image_payload("img-charge", text="hi")))
    charge_spy.assert_called_once()
    assert charge_spy.call_args.kwargs["model"] == fresh_db.image_understanding_model


# ---------- 计费 db 级：固定贝壳、幂等、账号隔离 ----------
def test_record_image_understanding_charge_fixed_and_idempotent(monkeypatch, fresh_db):
    import app.db as db

    user = db.create_or_get_platform_user_by_phone(phone="13800000001")
    bundle = db.get_or_create_default_ai4all_account_for_user(
        platform_user_id=user["id"], display_name="计费账号"
    )
    account_id = bundle["account"]["id"]

    first = db.record_image_understanding_charge(
        account_id=account_id,
        source_id="msg-1",
        idempotency_key=f"image-understanding-{account_id}-msg-1",
        model="qwen3-vl-plus",
    )
    assert first is not None
    assert first["cost_event"]["cost_type"] == "image_understanding"
    assert first["cost_event"]["computed_shell_micros"] == 5_000_000
    balance_after_first = first["wallet"]["balance_shell_micros"]

    # 同 idempotency_key 重放，不重复扣减。
    second = db.record_image_understanding_charge(
        account_id=account_id,
        source_id="msg-1",
        idempotency_key=f"image-understanding-{account_id}-msg-1",
        model="qwen3-vl-plus",
    )
    assert second["wallet"]["balance_shell_micros"] == balance_after_first

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM cost_events WHERE account_id=? AND cost_type='image_understanding'",
            (account_id,),
        ).fetchone()["c"]
    assert count == 1


# ---------- 多机：内联字节优先于本地路径 ----------
def test_inline_bytes_preferred_over_path(monkeypatch, fresh_db):
    """node 传来的 data_base64 应优先于 path 交给 VL（中心读不到 node 本地路径）。"""
    import base64 as _b64

    cap = _setup(monkeypatch, fresh_db, describe_return="一束花，暖色调")
    b64 = _b64.b64encode(b"fake-image-bytes").decode("ascii")
    res = turn_service.handle_openclaw_turn(
        OpenClawTurnRequest(
            **_image_payload("img-bytes", text="看这个", path="/node/local/only.jpg", data_base64=b64, fmt="image/jpeg")
        )
    )
    assert res.status == "ok"
    call = cap["describe_calls"][-1]
    assert call["image_b64"] == b64, "内联字节应优先传给 VL"
    assert call["image_format"] == "image/jpeg"
    # path 仍随 payload 透传（单机回退/排查），但优先级在 describe_image 内部决定。
    assert call["image_path"] == "/node/local/only.jpg"


# ---------- describe_image: base64 字节构图（_data_url_from_b64）----------
def _iu_with_settings(monkeypatch, *, max_bytes=10_485_760):
    """给 image_understanding 注入精简 settings（不触网，仅测构图分支）。"""
    import types
    import app.image_understanding as iu

    fake = types.SimpleNamespace(
        dashscope_api_key="",  # 空 key：describe_image 早返回 None，聚焦测 _data_url_from_b64
        image_max_bytes=max_bytes,
        image_inbound_dir="~/.openclaw/media/inbound",
        image_understanding_model="qwen3-vl-plus",
        image_understanding_timeout_seconds=30.0,
    )
    monkeypatch.setattr(iu, "settings", fake)
    return iu


def test_data_url_from_b64_normal(monkeypatch):
    import base64 as _b64

    iu = _iu_with_settings(monkeypatch)
    b64 = _b64.b64encode(b"\xff\xd8\xff\x00abc").decode("ascii")
    assert iu._data_url_from_b64(b64, "image/png") == f"data:image/png;base64,{b64}"
    # 裸 "image" / "png" 规整成 MIME。
    assert iu._data_url_from_b64(b64, "image").startswith("data:image/jpeg;base64,")
    assert iu._data_url_from_b64(b64, "png").startswith("data:image/png;base64,")


def test_data_url_from_b64_oversize_returns_none(monkeypatch):
    import base64 as _b64

    iu = _iu_with_settings(monkeypatch, max_bytes=4)
    b64 = _b64.b64encode(b"way-too-many-bytes").decode("ascii")
    assert iu._data_url_from_b64(b64, "image/jpeg") is None


def test_data_url_from_b64_bad_input_returns_none(monkeypatch):
    iu = _iu_with_settings(monkeypatch)
    assert iu._data_url_from_b64("!!!not-base64!!!", "image/jpeg") is None
    assert iu._data_url_from_b64("", "image/jpeg") is None
    assert iu._data_url_from_b64("   ", "image/jpeg") is None
