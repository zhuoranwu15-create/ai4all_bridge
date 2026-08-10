"""Plum Create 立绘上传：扩展格式、标准化、CSRF 与 owner 隔离。"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pillow_heif

from app.platform.media import assets
from app.platform.media.persistence import get_media_asset
from app.products.plum.api import app as plum_api
from app.products.plum.api import deps as plum_deps
from app.products.plum.api import media as creator_media_api
from app.products.plum.infrastructure import repository
from app.products.plum.manifest import install_public_routes


def _configure_plum(monkeypatch, fresh_db, *, dev_mode: bool) -> MagicMock:
    config = MagicMock(wraps=fresh_db)
    config.app_env = "test" if dev_mode else "production"
    config.plum_enabled = True
    config.plum_dev_mode = dev_mode
    config.plum_test_user_id = "user_plum_test"
    config.plum_test_phone = "plum-test@local.invalid"
    config.plum_fast_provider_id = "deepseek"
    config.plum_balanced_provider_id = "chatgpt"
    config.plum_immersive_provider_id = "deepseek-v4-pro"
    config.plum_public_test_auth_enabled = True
    config.plum_session_cookie_name = "plum_session"
    config.plum_csrf_cookie_name = "plum_csrf"
    config.plum_session_days = 30
    config.plum_session_cookie_secure = False
    config.media_image_max_bytes = fresh_db.media_image_max_bytes
    config.media_pending_ttl_hours = fresh_db.media_pending_ttl_hours
    monkeypatch.setattr(repository, "settings", config)
    monkeypatch.setattr(plum_api, "settings", config)
    monkeypatch.setattr(plum_deps, "settings", config)
    monkeypatch.setattr(creator_media_api, "settings", config)
    return config


def _heif_bytes() -> bytes:
    buffer = BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", (10, 16), color="purple")).save(buffer)
    return buffer.getvalue()


def _webp_bytes(*, alpha: bool) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    color = (20, 40, 80, 96) if alpha else (20, 40, 80)
    buffer = BytesIO()
    Image.new(mode, (12, 18), color=color).save(buffer, format="WEBP")
    return buffer.getvalue()


def _upload(client: TestClient, content: bytes, filename: str, mime: str, headers=None):
    return client.post(
        "/api/v1/products/plum/creator/media/uploads",
        headers=headers or {},
        files={"file": (filename, content, mime)},
        data={"kind": "image", "purpose": "character_portrait"},
    )


def test_creator_upload_standardizes_heif_and_webp(
    fresh_db, monkeypatch
):
    _configure_plum(monkeypatch, fresh_db, dev_mode=True)
    repository.seed_plum_dev()
    app = FastAPI()
    install_public_routes(app)

    with TestClient(app) as client:
        heif = _upload(client, _heif_bytes(), "portrait.heic", "image/heic")
        assert heif.status_code == 200, heif.text
        heif_media = heif.json()["media"]
        assert heif_media["mime"] == "image/jpeg"
        assert (heif_media["width"], heif_media["height"]) == (10, 16)
        heif_preview = client.get(heif_media["preview_url"])
        assert heif_preview.status_code == 200
        assert heif_preview.headers["content-type"].startswith("image/jpeg")

        webp = _upload(client, _webp_bytes(alpha=True), "portrait.webp", "image/webp")
        assert webp.status_code == 200, webp.text
        webp_media = webp.json()["media"]
        assert webp_media["mime"] == "image/png"
        preview = client.get(webp_media["preview_url"])
        reopened = Image.open(BytesIO(preview.content))
        assert reopened.mode == "RGBA"
        assert reopened.getpixel((0, 0))[3] < 255

        asset = get_media_asset(
            media_id=webp_media["media_id"],
            owner_platform_user_id="user_plum_test",
        )
        assert asset is not None and asset["status"] == "pending"
        assert Image.open(BytesIO(assets.read_media_file(asset["storage_path"]))).format == "PNG"


def test_creator_upload_requires_csrf_and_preview_is_owner_only(
    fresh_db, monkeypatch
):
    _configure_plum(monkeypatch, fresh_db, dev_mode=False)
    first_invite = repository.create_plum_access_invite(label="creator one")
    second_invite = repository.create_plum_access_invite(label="creator two")
    app = FastAPI()
    install_public_routes(app)

    with TestClient(app) as first:
        login = first.post(
            "/api/v1/products/plum/auth/access-code",
            json={"access_code": first_invite["access_code"], "display_name": "Alice"},
        )
        assert login.status_code == 200
        missing_csrf = _upload(first, _webp_bytes(alpha=False), "portrait.webp", "image/webp")
        assert missing_csrf.status_code == 403
        csrf = first.cookies.get("plum_csrf")
        uploaded = _upload(
            first,
            _webp_bytes(alpha=False),
            "portrait.webp",
            "image/webp",
            headers={"X-Plum-CSRF": csrf},
        )
        assert uploaded.status_code == 200, uploaded.text
        preview_url = uploaded.json()["media"]["preview_url"]
        assert first.get(preview_url).status_code == 200

    with TestClient(app) as second:
        login = second.post(
            "/api/v1/products/plum/auth/access-code",
            json={"access_code": second_invite["access_code"], "display_name": "Bob"},
        )
        assert login.status_code == 200
        denied = second.get(preview_url)
        assert denied.status_code == 404
        assert denied.json()["detail"] == "creator_media_not_found"
