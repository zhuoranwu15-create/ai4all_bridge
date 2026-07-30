"""S3 图文动态：发布带图动态的门控、认领原子性、幂等，以及主人/访客两条读路径。

上传走真实 ``POST /media/uploads``（图片上传门控 = 聊天图片位 or 动态图片位），
发布走 ``POST /v1/worlds/home/feed/posts``，访客读走 ``GET /v1/visits/{id}/feed``。不触网。
"""
from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO

from PIL import Image

import app.db as db
from app.platform.media.persistence import get_media_asset
from app.products.zhaoxi.application.companion_world_visits import (
    CompanionWorldVisitService,
)

NOW = datetime(2026, 7, 23, 12, 0, 0)


def _login(client, phone: str) -> tuple[dict, str]:
    verification = db.create_phone_verification(
        phone=phone, code="999999", expires_minutes=10
    )
    verified = db.set_verification_verified(
        verification["id"], token_expires_minutes=10
    )["verified_token"]
    response = client.post(
        "/v1/auth/session", json={"phone": phone, "verified_token": verified}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return (
        {"Authorization": f"Bearer {payload['access_token']}"},
        payload["platform_user"]["id"],
    )


def _seed_catalog() -> None:
    for rank in range(1, 5):
        db.create_character_template(
            template_id=f"tmpl_feedmedia_{rank}",
            source_type="operations",
            name=f"图文角色{rank}",
            avatar_ref=f"asset://feedmedia-{rank}",
            summary=f"图文简介{rank}",
            tags_json=json.dumps(["温柔", "好奇", f"类型{rank}"], ensure_ascii=False),
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n图文人格{rank}",
                    "IDENTITY.md": f"# IDENTITY\n\n- 你的名字是图文角色{rank}",
                },
                ensure_ascii=False,
            ),
            persona_version=f"v{rank}",
            initial_candidate_rank=rank,
        )


def _enable(monkeypatch, fresh_db, *, feed_image: bool = True) -> None:
    """打开 P1 + Feed + 动态图片位，并配好签名 secret（签不出 URL 时 url 会是 null）。"""
    fresh_db.companion_world_p1_enabled = True
    fresh_db.companion_world_feed_enabled = True
    fresh_db.companion_world_feed_image_enabled = feed_image
    for module in (
        "app.products.zhaoxi.api.media",
        "app.products.zhaoxi.api.companion_world",
    ):
        monkeypatch.setattr(
            f"{module}.settings.companion_world_feed_image_enabled", feed_image
        )
    monkeypatch.setattr(
        "app.platform.media.access.settings.media_url_signing_secret", "test-secret"
    )


def _ready_world(client, fresh_db, phone: str, *, seed: bool = True):
    if seed:
        _seed_catalog()
    headers, platform_user_id = _login(client, phone)
    candidates = client.post(
        "/v1/worlds/home/bootstrap", headers=headers
    ).json()["data"]["candidates"]
    response = client.post(
        "/v1/worlds/home/residents/confirm",
        headers=headers,
        json={"selections": [{"template_id": candidates[0]["template_id"]}]},
    )
    assert response.status_code == 200, response.text
    return headers, platform_user_id


def _jpeg(size=(12, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color="green").save(buffer, format="JPEG")
    return buffer.getvalue()


def _upload_image(client, headers, size=(12, 8)) -> str:
    response = client.post(
        "/v1/media/uploads",
        headers=headers,
        files={"file": ("photo.jpg", _jpeg(size), "image/jpeg")},
        data={"kind": "image"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["media_id"]


def _enable_visits(monkeypatch) -> None:
    """打开拜访能力，并把访客侧的"现在"钉在 NOW（visit 有效期 30 天，剩余时长可预期）。"""
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world_visits.settings"
        ".companion_world_visits_enabled",
        True,
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world_visits.beijing_naive_now", lambda: NOW
    )


def _active_visit(owner_platform_user_id: str, visitor_platform_user_id: str) -> str:
    """真实走 invite → redeem → accept，返回 active visit id。"""
    service = CompanionWorldVisitService()
    invitation = service.create_invite(owner_platform_user_id, now=NOW)
    visit = service.redeem(visitor_platform_user_id, code=invitation["code"], now=NOW)
    service.accept(owner_platform_user_id, visit_id=visit["id"], now=NOW)
    return str(visit["id"])


def _publish(client, headers, *, request_id: str, text: str = "", media_refs=()):
    return client.post(
        "/v1/worlds/home/feed/posts",
        headers=headers,
        json={
            "client_request_id": request_id,
            "text": text,
            "media_refs": list(media_refs),
        },
    )


def test_publish_feed_post_with_images_claims_assets_and_projects_urls(
    client, fresh_db, monkeypatch
):
    """两图动态：content.type=image、images 按发布顺序、资产转 referenced 且不再过期。"""
    _enable(monkeypatch, fresh_db)
    headers, platform_user_id = _ready_world(client, fresh_db, "19911120001")
    first = _upload_image(client, headers, size=(12, 8))
    second = _upload_image(client, headers, size=(20, 30))

    response = _publish(
        client,
        headers,
        request_id="feedmedia-001",
        text="今天的两张照片",
        media_refs=[first, second],
    )
    assert response.status_code == 201, response.text
    content = response.json()["data"]["post"]["content"]
    assert content["type"] == "image"
    assert content["text"] == "今天的两张照片"
    assert [item["media_id"] for item in content["images"]] == [first, second]
    # 尺寸来自 media_assets（库里不存第二份），URL 是 owner scope 的短 TTL 签名地址。
    assert content["images"][1]["width"] == 20 and content["images"][1]["height"] == 30
    assert f"scope=pu:{platform_user_id}" in content["images"][0]["url"]

    for media_id in (first, second):
        asset = get_media_asset(
            media_id=media_id, owner_platform_user_id=platform_user_id
        )
        assert asset["status"] == "referenced"
        assert asset["expires_at"] is None


def test_home_feed_list_returns_images_with_owner_scope(client, fresh_db, monkeypatch):
    """主人 Feed 列表逐条现签 owner scope URL；纯文字动态仍是 text 形状。"""
    _enable(monkeypatch, fresh_db)
    headers, platform_user_id = _ready_world(client, fresh_db, "19911120002")
    media_id = _upload_image(client, headers)
    assert _publish(client, headers, request_id="feedmedia-t01", text="只有文字").status_code == 201
    assert _publish(
        client, headers, request_id="feedmedia-i01", media_refs=[media_id]
    ).status_code == 201

    response = client.get("/v1/worlds/home/feed", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    by_content = {item["content"]["type"] for item in items}
    assert {"image", "text"} <= by_content
    image_item = next(item for item in items if item["content"]["type"] == "image")
    # 只发图：caption 是空串，不是 null。
    assert image_item["content"]["text"] == ""
    assert image_item["content"]["images"][0]["media_id"] == media_id
    assert f"scope=pu:{platform_user_id}" in image_item["content"]["images"][0]["url"]
    text_item = next(item for item in items if item["content"]["type"] == "text")
    assert "images" not in text_item["content"]


def test_publish_feed_post_rejects_media_when_flag_off(client, fresh_db, monkeypatch):
    """动态图片位关着时，即便资产合法也拒发（上传位由聊天图片位单独控制）。"""
    _enable(monkeypatch, fresh_db, feed_image=True)
    headers, _ = _ready_world(client, fresh_db, "19911120003")
    media_id = _upload_image(client, headers)
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world.settings"
        ".companion_world_feed_image_enabled",
        False,
    )
    response = _publish(
        client, headers, request_id="feedmedia-off1", media_refs=[media_id]
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "media_disabled"


def test_publish_feed_post_rejects_foreign_and_reused_assets(
    client, fresh_db, monkeypatch
):
    """跨用户资产与已挂在别的动态上的资产都收敛成 media_ref_invalid，且不留半条动态。"""
    _enable(monkeypatch, fresh_db)
    owner_headers, _ = _ready_world(client, fresh_db, "19911120004")
    other_headers, _ = _ready_world(client, fresh_db, "19911120005", seed=False)
    foreign = _upload_image(client, other_headers)
    response = _publish(
        client, owner_headers, request_id="feedmedia-frn1", media_refs=[foreign]
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "media_ref_invalid"

    mine = _upload_image(client, owner_headers)
    assert _publish(
        client, owner_headers, request_id="feedmedia-use1", media_refs=[mine]
    ).status_code == 201
    reuse = _publish(
        client, owner_headers, request_id="feedmedia-use2", media_refs=[mine]
    )
    assert reuse.status_code == 409, reuse.text
    assert reuse.json()["code"] == "media_ref_invalid"
    # 第二条动态整体回滚：Feed 里只有第一条带图动态。
    items = client.get("/v1/worlds/home/feed", headers=owner_headers).json()["data"][
        "items"
    ]
    assert [item["content"]["type"] for item in items].count("image") == 1


def test_publish_feed_post_media_limits_and_replay(client, fresh_db, monkeypatch):
    """超 4 张、空内容按 422 收敛；同 request_id 同图重放是幂等 200，不重复认领。"""
    _enable(monkeypatch, fresh_db)
    headers, _ = _ready_world(client, fresh_db, "19911120006")
    five = [_upload_image(client, headers) for _ in range(5)]
    over = _publish(client, headers, request_id="feedmedia-lim1", media_refs=five)
    assert over.status_code == 422, over.text
    assert over.json()["code"] == "invalid_request"

    empty = _publish(client, headers, request_id="feedmedia-emp1")
    assert empty.status_code == 422
    assert empty.json()["code"] == "invalid_request"

    first = _publish(
        client, headers, request_id="feedmedia-rep1", text="图", media_refs=five[:2]
    )
    assert first.status_code == 201, first.text
    replay = _publish(
        client, headers, request_id="feedmedia-rep1", text="图", media_refs=five[:2]
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["post"]["post_id"] == (
        first.json()["data"]["post"]["post_id"]
    )
    assert [
        item["media_id"] for item in replay.json()["data"]["post"]["content"]["images"]
    ] == five[:2]
    # 换图重发同一个 request_id 是内容冲突，不是重放。
    conflict = _publish(
        client, headers, request_id="feedmedia-rep1", text="图", media_refs=five[2:4]
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "idempotency_conflict"


# ------------------------------------------------------------- 访客读路径


def _visitor_feed_image(client, visitor_headers, visit_id: str) -> dict:
    response = client.get(f"/v1/visits/{visit_id}/feed", headers=visitor_headers)
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    item = next(one for one in items if one["content"]["type"] == "image")
    return item["content"]["images"][0]


def test_visitor_feed_signs_images_with_visit_scope(client, fresh_db, monkeypatch):
    """访客看主人的图文动态：URL 必须是 visit scope，且签名本身即可取到字节。"""
    _enable(monkeypatch, fresh_db)
    _enable_visits(monkeypatch)
    owner_headers, owner_id = _ready_world(client, fresh_db, "19911120007")
    visitor_headers, visitor_id = _login(client, "19911120008")
    media_id = _upload_image(client, owner_headers, size=(24, 16))
    assert _publish(
        client,
        owner_headers,
        request_id="feedmedia-vis1",
        text="给密友看的照片",
        media_refs=[media_id],
    ).status_code == 201
    visit_id = _active_visit(owner_id, visitor_id)

    image = _visitor_feed_image(client, visitor_headers, visit_id)
    assert image["media_id"] == media_id
    assert image["width"] == 24 and image["height"] == 16
    # 图属于主人、不属于访客：只能签 visit scope，绝不能漏出主人的 pu: scope。
    assert f"scope=visit:{visit_id}" in image["url"]
    assert f"scope=pu:{owner_id}" not in image["url"]

    # 签名 URL 是唯一凭据（不带 Authorization）；访客侧一律 no-store。
    fetched = client.get(image["url"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.headers["Cache-Control"] == "no-store, private"


def test_visitor_feed_image_url_dies_when_visit_revoked(
    client, fresh_db, monkeypatch
):
    """主人收回拜访后，此前签出的动态图 URL 立刻失效（§3.3 隐私红线）。"""
    _enable(monkeypatch, fresh_db)
    _enable_visits(monkeypatch)
    owner_headers, owner_id = _ready_world(client, fresh_db, "19911120009")
    visitor_headers, visitor_id = _login(client, "19911120010")
    media_id = _upload_image(client, owner_headers)
    assert _publish(
        client,
        owner_headers,
        request_id="feedmedia-vis2",
        media_refs=[media_id],
    ).status_code == 201
    visit_id = _active_visit(owner_id, visitor_id)
    url = _visitor_feed_image(client, visitor_headers, visit_id)["url"]
    assert client.get(url).status_code == 200

    CompanionWorldVisitService().terminate(
        owner_id, visit_id=visit_id, action="revoke", now=NOW
    )
    dead = client.get(url)
    assert dead.status_code == 403, dead.text
    assert dead.json()["code"] == "media_access_denied"
    # Feed 本身也不再可读，两道闸各自独立生效。
    assert client.get(
        f"/v1/visits/{visit_id}/feed", headers=visitor_headers
    ).status_code == 409
