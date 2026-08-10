"""S2 会话媒体：AI 会话与真人会话的图片/语音发送、认领、读模型与预览。

红线在 D-2：``messages.content`` 是喂模型的上下文（图片轮含 VL 描述），
``content_json`` 只存用户自己写的 caption。任何一条把 VL 描述漏给客户端的路径都是 bug。
上传走真实 ``POST /media/uploads``；只有 VL/ASR 两个外部 provider 被 mock，不触网。
"""
from __future__ import annotations

import json

import pytest
from datetime import datetime
from io import BytesIO

from PIL import Image

import app.db as db
from app.platform.media.persistence import get_media_asset, list_media_assets_unscoped
from app.products.mingchan.application import SqlCompanionWorldRepository
from app.products.mingchan.application.visits import CompanionWorldVisitService
from app.products.mingchan.domain.companion_world import CompanionWorldService

NOW = datetime(2026, 7, 23, 12, 0, 0)
VL_DESCRIPTION = "一只橘猫趴在窗台上，阳光温暖"


def _login(client, phone: str) -> tuple[dict, str]:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/api/v1/products/mingchan/auth/session", json={"phone": phone, "verified_token": verified}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return (
        {"Authorization": f"Bearer {payload['access_token']}"},
        payload["platform_user"]["id"],
    )


def _enable_media(monkeypatch, fresh_db, *, image: bool = True, voice: bool = True) -> None:
    """打开 P1 与聊天媒体位；上传门控与聊天门控读的是同两个开关，一并设置。"""
    fresh_db.mingchan_p1_enabled = True
    fresh_db.mingchan_chat_image_enabled = image
    fresh_db.mingchan_chat_voice_enabled = voice
    for flag, value in (
        ("mingchan_chat_image_enabled", image),
        ("mingchan_chat_voice_enabled", voice),
        ("mingchan_feed_image_enabled", False),
    ):
        monkeypatch.setattr(f"app.products.mingchan.api.media.settings.{flag}", value)


def _jpeg(size=(12, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color="blue").save(buffer, format="JPEG")
    return buffer.getvalue()


def _upload_image(client, headers) -> str:
    response = client.post(
        "/api/v1/products/mingchan/media/uploads",
        headers=headers,
        files={"file": ("photo.jpg", _jpeg(), "image/jpeg")},
        data={"kind": "image"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["media_id"]


def _upload_voice(client, headers, monkeypatch, *, transcript: str | None) -> str:
    monkeypatch.setattr(
        "app.products.mingchan.api.media.transcribe_audio", lambda **kwargs: transcript
    )
    response = client.post(
        "/api/v1/products/mingchan/media/uploads",
        headers=headers,
        files={"file": ("clip.m4a", b"\x00\x00\x00\x18ftypM4A " + b"\x33" * 128, "audio/m4a")},
        data={"kind": "voice", "duration_ms": "2400"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["media_id"]


# ---------------------------------------------------------------- AI 会话


def _seed_catalog() -> None:
    # rank 必须是连续 1..N 且不少于 MIN_INITIAL_CANDIDATES(4)，否则 bootstrap 报目录未就绪。
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_media_{rank}",
            source_type="operations",
            name=f"媒体角色{rank}",
            avatar_ref=f"asset://media-{rank}",
            summary=f"媒体角色简介{rank}",
            tags_json=json.dumps(["a", "b", "c"]),
            persona_seed_json=json.dumps(
                {"SOUL.md": f"# SOUL\n\n人格{rank}", "IDENTITY.md": f"# IDENTITY\n\n角色{rank}"},
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _confirm_resident(client, headers):
    candidates = client.post(
        "/api/v1/products/mingchan/worlds/home/bootstrap", headers=headers
    ).json()["data"]["candidates"]
    response = client.post(
        "/api/v1/products/mingchan/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": [{"template_id": candidates[0]["template_id"]}]},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["residents"][0]


def _target(platform_user_id: str, conversation_id: str):
    return CompanionWorldService(SqlCompanionWorldRepository()).resolve_conversation(
        platform_user_id, conversation_id
    )


def _ai_setup(client, fresh_db, monkeypatch, phone: str):
    _enable_media(monkeypatch, fresh_db)
    _seed_catalog()
    headers, platform_user_id = _login(client, phone)
    resident = _confirm_resident(client, headers)
    return headers, platform_user_id, _target(platform_user_id, resident["conversation_id"])


def _last_user_message(account_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT content, content_json, media_id FROM messages "
            "WHERE account_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    return dict(row)


def test_ai_image_turn_keeps_vl_description_out_of_display_payload(
    client, fresh_db, monkeypatch
):
    """D-2 红线：VL 描述只进 content（喂模型），content_json / 历史 / 预览都只能是 caption。"""
    headers, platform_user_id, target = _ai_setup(client, fresh_db, monkeypatch, "19965401001")
    fresh_db.image_understanding_enabled = True
    described = []

    def _describe(**kwargs):
        described.append(kwargs)
        return VL_DESCRIPTION

    monkeypatch.setattr("app.agent_runtime.turns.service.describe_image", _describe)
    image_charges = []
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.record_image_understanding_charge",
        lambda **kwargs: image_charges.append(kwargs),
    )
    media_id = _upload_image(client, headers)

    turn = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "media_turn_0001", "text": "这是哪", "media_ref": media_id},
    )
    assert turn.status_code == 200, turn.text

    # VL 走内联字节（媒体库不在 describe_image 的本地路径白名单内）。
    assert described and described[-1]["image_b64"]
    assert described[-1]["image_path"] is None
    assert described[-1]["caption"] == "这是哪"
    assert image_charges
    assert image_charges[-1]["registry"].require_enabled("mingchan").app_id == "mingchan"

    stored = _last_user_message(target.runtime_account_id)
    assert VL_DESCRIPTION in stored["content"], "上下文文本必须含 VL 描述"
    assert stored["media_id"] == media_id
    assert json.loads(stored["content_json"]) == {"type": "image", "text": "这是哪"}

    # 资产在同一事务内被认领：状态翻转且不再有回收截止时间。
    asset = get_media_asset(media_id=media_id, owner_platform_user_id=platform_user_id)
    assert asset["status"] == "referenced" and asset["expires_at"] is None

    history = client.get(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/messages", headers=headers
    )
    assert history.status_code == 200, history.text
    assert VL_DESCRIPTION not in history.text, "VL 描述绝不能下发给客户端"
    user_item = [item for item in history.json()["data"]["messages"] if item["role"] == "user"][0]
    content = user_item["content"]
    assert content["type"] == "image"
    assert content["text"] == "这是哪"
    assert content["media_id"] == media_id
    assert (content["width"], content["height"]) == (12, 8)
    assert content["url"].startswith(
        f"/api/v1/products/mingchan/media/{media_id}?exp="
    )
    assert f"scope=pu:{platform_user_id}" in content["url"]
    # 老字段是 content 的镜像，不是另一份口径。
    assert (user_item["message_type"], user_item["text"]) == ("image", "这是哪")

    listed = client.get("/api/v1/products/mingchan/conversations", headers=headers)
    previews = [item["last_preview"] for item in listed.json()["data"]["items"]]
    assert VL_DESCRIPTION not in " ".join(str(p) for p in previews)


def test_ai_image_turn_without_caption_previews_placeholder(client, fresh_db, monkeypatch):
    """无 caption 的图片消息不能在会话列表里留空白气泡。"""
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401002")
    fresh_db.image_understanding_enabled = True
    monkeypatch.setattr(
        "app.agent_runtime.turns.service.describe_image", lambda **kwargs: VL_DESCRIPTION
    )
    media_id = _upload_image(client, headers)

    turn = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "media_turn_0002", "media_ref": media_id},
    )
    assert turn.status_code == 200, turn.text

    listed = client.get("/api/v1/products/mingchan/conversations", headers=headers)
    item = [
        row
        for row in listed.json()["data"]["items"]
        if row["conversation_id"] == target.conversation_id
    ][0]
    assert item["last_preview"] in {"[图片]", "mock reply"}, item["last_preview"]
    assert VL_DESCRIPTION not in listed.text


def test_ai_voice_turn_feeds_transcript_and_surfaces_it(client, fresh_db, monkeypatch):
    """D-5：转写既是模型上下文，也是可下发给客户端的"用户自己的话"。"""
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401003")
    media_id = _upload_voice(client, headers, monkeypatch, transcript="今天挺累的")

    turn = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "voice_turn_0001", "media_ref": media_id},
    )
    assert turn.status_code == 200, turn.text
    assert turn.json()["data"]["reply"]["text"] == "mock reply", "有转写就要走主模型"

    stored = _last_user_message(target.runtime_account_id)
    assert stored["content"] == "今天挺累的"
    assert json.loads(stored["content_json"]) == {"type": "audio", "text": ""}

    history = client.get(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/messages", headers=headers
    )
    content = [
        item for item in history.json()["data"]["messages"] if item["role"] == "user"
    ][0]["content"]
    assert content["type"] == "audio"
    assert content["transcript"] == "今天挺累的"
    assert content["duration_ms"] == 2400
    assert content["text"] == "", "caption 为空就是空，不能拿转写冒充用户写的字"


def test_ai_voice_turn_without_transcript_falls_back_without_main_model(
    client, fresh_db, monkeypatch
):
    """转写失败且无 caption：走兜底话术，不让主模型对着空内容瞎猜。"""
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401004")
    media_id = _upload_voice(client, headers, monkeypatch, transcript=None)

    turn = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "voice_turn_0002", "media_ref": media_id},
    )
    assert turn.status_code == 200, turn.text
    assert turn.json()["data"]["reply"]["text"] == fresh_db.voice_message_fallback_text


def test_ai_turn_media_ref_is_single_use_and_owner_anchored(client, fresh_db, monkeypatch):
    """一份资产只能发一次；别人的 media_ref 与不存在的收敛成同一个码。"""
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401005")
    fresh_db.image_understanding_enabled = False
    other_headers, _other = _login(client, "19965401006")
    media_id = _upload_image(client, headers)

    first = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "single_use_0001", "media_ref": media_id},
    )
    assert first.status_code == 200, first.text

    reused = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "single_use_0002", "media_ref": media_id},
    )
    assert reused.json()["code"] == "media_ref_invalid"
    # 认领失败必须把入站消息一起回滚，不留半条消息。
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE account_id = ? AND message_id = ?",
            (target.runtime_account_id, f"app:{target.conversation_id}:single_use_0002"),
        ).fetchone()["c"]
    assert count == 0

    stolen = _upload_image(client, other_headers)
    cross = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "cross_owner_0001", "media_ref": stolen},
    )
    assert cross.json()["code"] == "media_ref_invalid"


def test_ai_turn_replay_with_same_media_ref_is_idempotent(client, fresh_db, monkeypatch):
    """重试是客户端的正常行为：资产已被自己认领过，不能报 media_ref_invalid。"""
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401007")
    fresh_db.image_understanding_enabled = False
    media_id = _upload_image(client, headers)
    body = {"client_message_id": "replay_media_001", "media_ref": media_id}

    first = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn", headers=headers, json=body
    )
    replay = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn", headers=headers, json=body
    )
    assert first.status_code == replay.status_code == 200, replay.text
    assert replay.json()["data"]["deduplicated"] is True


def test_ai_turn_requires_text_or_media_and_respects_kind_gate(client, fresh_db, monkeypatch):
    headers, _pu, target = _ai_setup(client, fresh_db, monkeypatch, "19965401008")
    empty = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "empty_turn_0001", "text": "   "},
    )
    assert empty.json()["code"] == "media_content_required"

    media_id = _upload_image(client, headers)
    fresh_db.mingchan_chat_image_enabled = False
    gated = client.post(
        f"/api/v1/products/mingchan/ai-conversations/{target.conversation_id}/turn",
        headers=headers,
        json={"client_message_id": "gated_turn_0001", "media_ref": media_id},
    )
    assert gated.json()["code"] == "media_disabled"


# ------------------------------------------------------------- 真人会话


def _enable_human(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.settings.mingchan_human_chat_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.products.mingchan.api.human_chat.beijing_naive_now", lambda: NOW
    )


def _active_conversation(owner_id: str, visitor_id: str) -> dict:
    world = db.get_or_create_home_universe(platform_user_id=owner_id)
    db.set_universe_onboarding_state(universe_id=world["id"], onboarding_state="confirmed")
    service = CompanionWorldVisitService()
    invitation = service.create_invite(owner_id, now=NOW)
    visit = service.redeem(visitor_id, code=invitation["code"], now=NOW)
    return service.accept(owner_id, visit_id=visit["id"], now=NOW)["conversation"]


def test_human_chat_image_is_visible_to_both_sides_with_scoped_urls(
    client, fresh_db, monkeypatch
):
    """自己的图签 ``pu:`` scope，对方的图只能签 ``visit:`` scope（visit 一结束即失效）。"""
    _enable_media(monkeypatch, fresh_db)
    _enable_human(monkeypatch)
    owner_headers, owner_id = _login(client, "19965402001")
    visitor_headers, visitor_id = _login(client, "19965402002")
    conversation = _active_conversation(owner_id, visitor_id)
    media_id = _upload_image(client, owner_headers)

    sent = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "human_media_001", "media_ref": media_id},
    )
    assert sent.status_code == 200, sent.text
    message = sent.json()["data"]["message"]
    assert sent.json()["data"]["created"] is True
    assert message["content"]["type"] == "image"
    assert message["content"]["text"] == "", "图片可以不带 caption"
    assert f"scope=pu:{owner_id}" in message["content"]["url"]

    assert get_media_asset(media_id=media_id, owner_platform_user_id=owner_id)["status"] == "referenced"

    seen = client.get(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages", headers=visitor_headers
    )
    assert seen.status_code == 200, seen.text
    content = seen.json()["data"]["items"][0]["content"]
    assert content["type"] == "image" and content["media_id"] == media_id
    assert f"scope=visit:{conversation['visit_id']}" in content["url"]
    # 访客拿签名 URL 能真的取到字节（签名是唯一凭据，不带 Authorization）。
    fetched = client.get(content["url"])
    assert fetched.status_code == 200
    assert fetched.headers["Cache-Control"] == "no-store, private"

    listed = client.get("/api/v1/products/mingchan/human-conversations", headers=visitor_headers)
    assert listed.json()["data"]["items"][0]["last_preview"] == "[图片]"


def test_human_chat_media_idempotency_conflicts_on_swapped_asset(
    client, fresh_db, monkeypatch
):
    """同一个 client_message_id 重放要幂等，换一张图必须报冲突。"""
    _enable_media(monkeypatch, fresh_db)
    _enable_human(monkeypatch)
    owner_headers, owner_id = _login(client, "19965402003")
    _visitor_headers, visitor_id = _login(client, "19965402004")
    conversation = _active_conversation(owner_id, visitor_id)
    first_media = _upload_image(client, owner_headers)
    second_media = _upload_image(client, owner_headers)
    body = {
        "client_message_id": "human_media_002",
        "text": "看这个",
        "media_ref": first_media,
    }

    created = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json=body,
    )
    assert created.status_code == 200, created.text
    replay = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json=body,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["created"] is False
    assert replay.json()["data"]["message"]["message_id"] == created.json()["data"]["message"]["message_id"]

    swapped = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={**body, "media_ref": second_media},
    )
    assert swapped.json()["code"] == "idempotency_conflict"
    # 冲突路径不认领第二张图，它仍可被回收。
    assert get_media_asset(
        media_id=second_media, owner_platform_user_id=owner_id
    )["status"] == "pending"


def test_human_chat_rejects_empty_message_and_foreign_media(client, fresh_db, monkeypatch):
    _enable_media(monkeypatch, fresh_db)
    _enable_human(monkeypatch)
    owner_headers, owner_id = _login(client, "19965402005")
    visitor_headers, visitor_id = _login(client, "19965402006")
    conversation = _active_conversation(owner_id, visitor_id)

    empty = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "human_media_003", "text": "  "},
    )
    assert empty.json()["code"] == "media_content_required"

    foreign = _upload_image(client, visitor_headers)
    stolen = client.post(
        f"/api/v1/products/mingchan/human-conversations/{conversation['id']}/messages",
        headers=owner_headers,
        json={"client_message_id": "human_media_004", "media_ref": foreign},
    )
    assert stolen.json()["code"] == "media_ref_invalid"
    assert list_media_assets_unscoped(media_ids=[foreign])[foreign]["status"] == "pending"
@pytest.fixture
def client(mingchan_client):
    return mingchan_client
