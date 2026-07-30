"""S1 媒体上传端点（``POST /media/uploads``）：门控、限额、白名单、EXIF 剥离、落库回收口径。

领域层的字节处理细节在 `tests/test_media_assets.py`；这里只验 HTTP 契约与库/磁盘副作用。
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

import app.db as db
from app.platform.media import assets
from app.platform.media.persistence import get_media_asset


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


def _enable(monkeypatch, *, image: bool = True, voice: bool = True, feed: bool = False) -> None:
    """打开 P1 与媒体能力位；三位分别可控，用于验 kind 级门控。"""
    monkeypatch.setattr(
        "app.products.zhaoxi.api.companion_world.settings.companion_world_p1_enabled", True
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.settings.companion_world_chat_image_enabled", image
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.settings.companion_world_chat_voice_enabled", voice
    )
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.settings.companion_world_feed_image_enabled", feed
    )


def _jpeg_with_gps(size=(12, 8)) -> bytes:
    image = Image.new("RGB", size, color="blue")
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x8825] = {1: "N", 2: (39.0, 54.0, 0.0), 3: "E", 4: (116.0, 23.0, 0.0)}
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _upload(client, headers, *, kind: str, content: bytes, name: str, mime: str, **form):
    return client.post(
        "/v1/media/uploads",
        headers=headers,
        files={"file": (name, content, mime)},
        data={"kind": kind, **form},
    )


def test_upload_image_strips_exif_and_creates_pending_asset(
    client, fresh_db, monkeypatch, test_settings
):
    _enable(monkeypatch)
    headers, platform_user_id = _login(client, "19965301001")

    response = _upload(
        client,
        headers,
        kind="image",
        content=_jpeg_with_gps(),
        name="photo.jpg",
        mime="image/jpeg",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "ok"
    data = body["data"]
    assert data["kind"] == "image"
    assert data["mime"] == "image/jpeg"
    assert (data["width"], data["height"]) == (12, 8)
    assert data["duration_ms"] is None and data["transcript"] is None
    # 未被引用的资产必须带回收截止时间；URL 过期是另一个更短的口径。
    assert data["expires_at"] and data["url_expires_at"]
    assert data["url"].startswith(f"/v1/media/{data['media_id']}?exp=")
    # 签名密钥属于凭证，任何响应体里都不能出现。
    assert test_settings.media_url_signing_secret not in response.text

    asset = get_media_asset(
        media_id=data["media_id"], owner_platform_user_id=platform_user_id
    )
    assert asset is not None
    assert asset["status"] == "pending"
    # D-7：S4 接阿里云前恒为 skipped，不能是会误导运营的 pending。
    assert asset["moderation_status"] == "skipped"
    stored = assets.read_media_file(str(asset["storage_path"]))
    assert Image.open(BytesIO(stored)).getexif() == {}
    assert b"GPS" not in stored


def test_upload_rejects_cross_owner_read_of_asset_row(client, fresh_db, monkeypatch):
    """账号隔离：另一个真人拿着同一个 media_id 也读不到资产行。"""
    _enable(monkeypatch)
    headers, _owner = _login(client, "19965301003")
    _other_headers, other_id = _login(client, "19965301004")

    media_id = _upload(
        client,
        headers,
        kind="image",
        content=_jpeg_with_gps(),
        name="photo.jpg",
        mime="image/jpeg",
    ).json()["data"]["media_id"]

    assert get_media_asset(media_id=media_id, owner_platform_user_id=other_id) is None


def test_upload_voice_stores_bytes_and_transcript(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.transcribe_audio",
        lambda **kwargs: "今天挺累的",
    )
    headers, platform_user_id = _login(client, "19965301005")
    raw = b"\x00\x00\x00\x18ftypM4A " + b"\x11" * 256

    response = _upload(
        client,
        headers,
        kind="voice",
        content=raw,
        name="clip.m4a",
        mime="audio/m4a",
        duration_ms="3200",
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["kind"] == "voice"
    assert data["mime"] == "audio/m4a"
    assert data["duration_ms"] == 3200
    assert data["transcript"] == "今天挺累的"
    asset = get_media_asset(
        media_id=data["media_id"], owner_platform_user_id=platform_user_id
    )
    assert assets.read_media_file(str(asset["storage_path"])) == raw, "语音不转码，原样存"


def test_upload_voice_survives_transcribe_failure(client, fresh_db, monkeypatch):
    """D-5：转写失败不阻塞发送，transcript 留空由上层落兜底话术。"""
    _enable(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.products.zhaoxi.api.media.transcribe_audio", _boom)
    headers, _pu = _login(client, "19965301006")

    response = _upload(
        client,
        headers,
        kind="voice",
        content=b"\x00\x00\x00\x18ftypM4A " + b"\x22" * 128,
        name="clip.m4a",
        mime="audio/m4a",
        duration_ms="1500",
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["transcript"] is None


@pytest.mark.parametrize(
    "kind,image,voice,feed",
    [
        ("image", False, True, False),  # 两个图片位都关 → 图片不可上传
        ("voice", True, False, False),
    ],
)
def test_upload_is_gated_per_kind(client, fresh_db, monkeypatch, kind, image, voice, feed):
    _enable(monkeypatch, image=image, voice=voice, feed=feed)
    headers, _pu = _login(client, "19965301007")

    response = _upload(
        client,
        headers,
        kind=kind,
        content=_jpeg_with_gps() if kind == "image" else b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64,
        name="x.bin",
        mime="application/octet-stream",
        duration_ms="1000",
    )

    assert response.status_code == 404
    assert response.json()["code"] == "media_disabled"


def test_upload_image_allowed_when_only_feed_flag_is_on(client, fresh_db, monkeypatch):
    """图片上传是共用地基：只开图文动态也应放行。"""
    _enable(monkeypatch, image=False, voice=False, feed=True)
    headers, _pu = _login(client, "19965301008")

    response = _upload(
        client,
        headers,
        kind="image",
        content=_jpeg_with_gps(),
        name="photo.jpg",
        mime="image/jpeg",
    )

    assert response.status_code == 200, response.text


def test_upload_rejects_oversized_and_empty_payloads(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.settings.media_image_max_bytes", 512
    )
    headers, _pu = _login(client, "19965301009")

    too_large = _upload(
        client,
        headers,
        kind="image",
        content=_jpeg_with_gps(size=(400, 400)),
        name="photo.jpg",
        mime="image/jpeg",
    )
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "media_too_large"

    empty = _upload(
        client, headers, kind="image", content=b"", name="photo.jpg", mime="image/jpeg"
    )
    assert empty.status_code == 422
    assert empty.json()["code"] == "media_content_required"


def test_upload_rejects_non_whitelisted_format_and_bad_kind(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    headers, _pu = _login(client, "19965301010")
    gif = BytesIO()
    Image.new("RGB", (8, 8), color="red").save(gif, format="GIF")

    unsupported = _upload(
        client, headers, kind="image", content=gif.getvalue(), name="a.gif", mime="image/gif"
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["code"] == "media_kind_unsupported"

    disguised = _upload(
        client,
        headers,
        kind="image",
        content=b"definitely-not-an-image" * 8,
        name="a.jpg",
        mime="image/jpeg",
    )
    assert disguised.status_code == 422
    assert disguised.json()["code"] == "media_decode_failed"

    bad_kind = _upload(
        client, headers, kind="video", content=b"abc", name="a.mp4", mime="video/mp4"
    )
    assert bad_kind.status_code == 415
    assert bad_kind.json()["code"] == "media_kind_unsupported"


def test_upload_rejects_voice_over_duration_limit(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.settings.media_voice_max_duration_ms", 60_000
    )
    headers, _pu = _login(client, "19965301011")

    response = _upload(
        client,
        headers,
        kind="voice",
        content=b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64,
        name="clip.m4a",
        mime="audio/m4a",
        duration_ms="90000",
    )

    assert response.status_code == 413
    assert response.json()["code"] == "media_duration_exceeded"


def test_upload_requires_session(client, fresh_db, monkeypatch):
    _enable(monkeypatch)

    response = _upload(
        client, {}, kind="image", content=_jpeg_with_gps(), name="p.jpg", mime="image/jpeg"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_upload_write_failure_leaves_no_orphan_file(client, fresh_db, monkeypatch):
    """写库失败必须就地删掉已落盘的文件——回收 job 按库行扫，扫不到无主文件。"""
    _enable(monkeypatch)
    headers, _pu = _login(client, "19965301012")
    written: list[str] = []
    real_write = assets.write_media_file

    def _capture(*, storage_path: str, data: bytes):
        written.append(storage_path)
        return real_write(storage_path=storage_path, data=data)

    monkeypatch.setattr("app.products.zhaoxi.api.media.write_media_file", _capture)
    monkeypatch.setattr(
        "app.products.zhaoxi.api.media.insert_media_asset",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    with pytest.raises(RuntimeError):
        _upload(
            client,
            headers,
            kind="image",
            content=_jpeg_with_gps(),
            name="photo.jpg",
            mime="image/jpeg",
        )

    assert written, "落盘应已发生，否则这条断言没验到清理"
    with pytest.raises(FileNotFoundError):
        assets.read_media_file(written[0])


def test_upload_is_rate_limited_per_user(client, fresh_db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr("app.products.zhaoxi.api.media._UPLOAD_RPM_LIMIT", 2)
    headers, _pu = _login(client, "19965301013")
    other_headers, _other = _login(client, "19965301014")

    for _ in range(2):
        assert (
            _upload(
                client,
                headers,
                kind="image",
                content=_jpeg_with_gps(),
                name="p.jpg",
                mime="image/jpeg",
            ).status_code
            == 200
        )
    limited = _upload(
        client, headers, kind="image", content=_jpeg_with_gps(), name="p.jpg", mime="image/jpeg"
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    # 限流按真人隔离，不能连坐。
    assert (
        _upload(
            client,
            other_headers,
            kind="image",
            content=_jpeg_with_gps(),
            name="p.jpg",
            mime="image/jpeg",
        ).status_code
        == 200
    )
