"""媒体资产领域层单测（v1.5 S1 / D-4）：EXIF 剥离、白名单、落盘路径。

这里不起 FastAPI、不碰 DB——纯字节流处理，跑得快、失败定位清楚。
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.platform.media import assets


def _use_tmp_storage(monkeypatch, tmp_path) -> None:
    """把落盘根目录指到临时目录：替换模块内的 settings 引用，不去改全局 settings 单例。"""
    monkeypatch.setattr(
        assets, "settings", SimpleNamespace(media_storage_dir=str(tmp_path / "media"))
    )


def _png_bytes(size=(8, 6), mode="RGB", **info) -> bytes:
    image = Image.new(mode, size, color="red")
    buffer = BytesIO()
    image.save(buffer, format="PNG", **info)
    return buffer.getvalue()


def _jpeg_with_gps(size=(8, 6)) -> bytes:
    """构造一张带 EXIF（含 GPS）的 JPEG，用 Pillow 自带的 Exif 容器，不引额外依赖。"""
    image = Image.new("RGB", size, color="blue")
    exif = Image.Exif()
    exif[0x010F] = "TestMake"  # Make
    exif[0x0112] = 1  # Orientation
    gps = {1: "N", 2: (39.0, 54.0, 0.0), 3: "E", 4: (116.0, 23.0, 0.0)}
    exif[0x8825] = gps  # GPSInfo
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_strip_image_metadata_drops_exif_and_gps():
    raw = _jpeg_with_gps()
    assert Image.open(BytesIO(raw)).getexif(), "构造的样本本身必须带 EXIF，否则断言无意义"

    normalized = assets.strip_image_metadata(raw)

    assert normalized.mime == "image/jpeg"
    assert (normalized.width, normalized.height) == (8, 6)
    reopened = Image.open(BytesIO(normalized.data))
    assert not reopened.getexif(), "重编码后不得残留任何 EXIF 标签"
    assert 0x8825 not in reopened.getexif()
    assert b"GPS" not in normalized.data


def test_strip_image_metadata_drops_png_text_chunks():
    raw = _png_bytes(pnginfo=_png_text_chunk())

    normalized = assets.strip_image_metadata(raw)

    assert normalized.mime == "image/png"
    assert b"secret-comment" not in normalized.data
    assert not Image.open(BytesIO(normalized.data)).text


def _png_text_chunk():
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret-comment")
    return info


def test_strip_image_metadata_preserves_icc_profile():
    # 用一段可辨识的假 profile：只验"被搬过去了"，不验色彩管理语义。
    fake_icc = b"\x00\x00\x02\x0cICC-FAKE" + b"\x00" * 100
    image = Image.new("RGB", (4, 4), color="green")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", icc_profile=fake_icc)

    normalized = assets.strip_image_metadata(buffer.getvalue())

    assert Image.open(BytesIO(normalized.data)).info.get("icc_profile") == fake_icc


def test_strip_image_metadata_applies_exif_orientation():
    """Orientation=6（顺时针 90°）必须被物理旋转，否则剥完 EXIF 竖拍照片会躺倒。"""
    image = Image.new("RGB", (10, 4), color="white")
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    normalized = assets.strip_image_metadata(buffer.getvalue())

    assert (normalized.width, normalized.height) == (4, 10)


def test_strip_image_metadata_converts_palette_png_without_color_shift():
    palette = Image.new("P", (4, 4))
    palette.putpalette([255, 0, 0] * 256)
    buffer = BytesIO()
    palette.save(buffer, format="PNG")

    normalized = assets.strip_image_metadata(buffer.getvalue())

    reopened = Image.open(BytesIO(normalized.data)).convert("RGB")
    assert reopened.getpixel((0, 0)) == (255, 0, 0), "调色板图不得因丢色板而串色"


@pytest.mark.parametrize("fmt", ["GIF", "WEBP", "BMP"])
def test_strip_image_metadata_rejects_non_whitelisted_formats(fmt):
    buffer = BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buffer, format=fmt)

    with pytest.raises(assets.MediaKindUnsupportedError) as err:
        assets.strip_image_metadata(buffer.getvalue())
    assert err.value.code == "media_kind_unsupported"


def test_strip_image_metadata_rejects_disguised_and_empty_payloads():
    # 改后缀伪装：内容根本不是图片。
    with pytest.raises(assets.MediaDecodeFailedError) as err:
        assets.strip_image_metadata(b"not-an-image-at-all" * 8)
    assert err.value.code == "media_decode_failed"

    with pytest.raises(assets.MediaDecodeFailedError):
        assets.strip_image_metadata(b"")


def test_strip_image_metadata_rejects_oversized_pixel_count(monkeypatch):
    monkeypatch.setattr(assets, "MAX_IMAGE_PIXELS", 10)

    with pytest.raises(assets.MediaTooLargeError) as err:
        assets.strip_image_metadata(_png_bytes(size=(8, 6)))
    assert err.value.code == "media_too_large"


def test_normalize_voice_trusts_magic_bytes_over_declared_type():
    m4a = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64

    normalized = assets.normalize_voice(
        raw=m4a, content_type="application/octet-stream", duration_ms=1200
    )

    assert normalized.mime == "audio/m4a"
    assert normalized.duration_ms == 1200
    assert normalized.data is m4a, "语音不转码，字节必须原样存"
    assert assets.voice_extension(normalized.mime) == ".m4a"


def test_normalize_voice_rejects_unknown_container():
    with pytest.raises(assets.MediaKindUnsupportedError):
        assets.normalize_voice(raw=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, content_type="image/png")
    with pytest.raises(assets.MediaDecodeFailedError):
        assets.normalize_voice(raw=b"", content_type="audio/m4a")


def test_media_id_and_storage_path_shape():
    media_id = assets.new_media_id()
    digest = assets.sha256_hex(b"payload")

    path = assets.build_storage_path(media_id=media_id, sha256=digest)

    assert media_id.startswith("mda_") and len(media_id) > 24
    assert path == f"{digest[0:2]}/{digest[2:4]}/{media_id}"
    assert assets.new_media_id() != media_id


def test_write_read_delete_roundtrip_is_atomic_and_private(tmp_path, monkeypatch):
    _use_tmp_storage(monkeypatch, tmp_path)
    digest = assets.sha256_hex(b"hello")
    path = assets.build_storage_path(media_id="mda_test", sha256=digest)

    written = assets.write_media_file(storage_path=path, data=b"hello")

    assert assets.read_media_file(path) == b"hello"
    assert oct(written.stat().st_mode)[-3:] == "600", "媒体文件只对服务进程可读"
    assert not list(written.parent.glob(".*tmp")), "临时文件必须已被 rename 掉"
    assert assets.delete_media_file(path) is True
    assert assets.delete_media_file(path) is False, "删除必须幂等"


def test_resolve_media_file_blocks_path_traversal(tmp_path, monkeypatch):
    _use_tmp_storage(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        assets.resolve_media_file("../../etc/passwd")
    with pytest.raises(ValueError):
        assets.resolve_media_file("ab/cd/../../../../etc/passwd")
    with pytest.raises(ValueError):
        assets.resolve_media_file("   ")
