"""媒体资产领域层：格式白名单、EXIF 剥离重编码、落盘与路径解析（v1.5 S1 / D-4）。

本模块只负责**内容处理与文件落盘**，不碰 DB、不认账号——账号隔离由调用方
（上传端点 + `media_assets` 仓储）用 `owner_platform_user_id` 保证。

三条硬约定：

1. **不信任客户端声明**。`kind` / `mime` / `width` / `height` 全部由服务端从字节流重新判定，
   客户端传什么 Content-Type 只作为白名单预筛。
2. **图片一律重编码**。EXIF / GPS / XMP / IPTC / PNG tEXt 随 `info` 字典整体丢弃，
   只有 ICC profile 显式搬回去（丢 ICC 会让广色域照片肉眼可见偏色，且 ICC 不含位置信息）。
3. **落盘路径由内容摘要决定**，存相对路径进库；换对象存储只需改本模块的解析函数，契约不动。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app.config import settings

logger = logging.getLogger("ai4all.media.assets")

MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_VOICE = "voice"

# 不透明 media_ref：沿用 resident_drafts.draft_token 的既有做法，不发明 HMAC 句柄。
_MEDIA_ID_PREFIX = "mda_"

# 共享聊天媒体保持原白名单；Plum Create 通过专用入口额外接受 WEBP / HEIF，
# 再统一落成浏览器稳定支持的 JPEG / PNG，避免扩大其他产品已有上传合同。
_IMAGE_MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png"}
_BASE_IMAGE_FORMATS = frozenset(_IMAGE_MIME_BY_FORMAT)
_CREATOR_PORTRAIT_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "HEIF", "HEIC"})

# 只注册 HEIF/HEIC opener，不注册 AVIF opener；AVIF 明确不在 Create V1 白名单内。
register_heif_opener()

# 像素上限：8MB 的字节闸门挡不住"低熵超大图"（一张纯渐变 100MP JPEG 可能只有几 MB），
# 而重编码要按像素分配内存。刻意不做成配置项，避免为一个安全网加开关。
MAX_IMAGE_PIXELS = 60_000_000

# 语音只做透传存储（不转码），因此白名单同 ASR 入口；octet-stream 交给下面的魔数嗅探。
_VOICE_MIME_WHITELIST = {
    "audio/aac": ".aac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
}


class MediaError(RuntimeError):
    """媒体处理失败的基类；``code`` 直接作为对外稳定错误码（见 plan §6）。"""

    code = "media_decode_failed"


class MediaKindUnsupportedError(MediaError):
    code = "media_kind_unsupported"


class MediaDecodeFailedError(MediaError):
    code = "media_decode_failed"


class MediaTooLargeError(MediaError):
    code = "media_too_large"


class MediaRefInvalidError(MediaError):
    """``media_ref`` 认领失败：不存在、不属于本人，或已被别的消息引用过。"""

    code = "media_ref_invalid"


@dataclass(frozen=True)
class NormalizedImage:
    """重编码后的图片：``data`` 是准备落盘的字节，``mime`` 是真实格式。"""

    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class NormalizedVoice:
    """语音原样存储；``mime`` 经魔数校正，``duration_ms`` 由客户端声明（同 ASR 入口）。"""

    data: bytes
    mime: str
    duration_ms: Optional[int]


def new_media_id() -> str:
    """生成不透明 media_ref：``mda_`` + token_urlsafe(24)。"""
    return f"{_MEDIA_ID_PREFIX}{secrets.token_urlsafe(24)}"


def sha256_hex(data: bytes) -> str:
    """落盘内容摘要；同时是分片目录来源。"""
    return hashlib.sha256(data).hexdigest()


def build_storage_path(*, media_id: str, sha256: str) -> str:
    """相对 ``media_storage_dir`` 的路径：``<sha[0:2]>/<sha[2:4]>/<media_id>``。

    刻意不带扩展名：真实格式在 `media_assets.mime` 里，文件名不承担类型语义，
    也就不存在"改扩展名骗读接口"的路径。
    """
    if len(sha256) < 4:
        raise ValueError("sha256 must be a full hex digest")
    return f"{sha256[0:2]}/{sha256[2:4]}/{media_id}"


def _target_mode(image_format: str, source: Image.Image) -> str:
    """挑一个能无损承载像素、且目标格式支持的 mode。

    P（调色板）模式刻意不保留：调色板不在 ``info`` 里，只 paste 像素会丢色板导致串色，
    统一先 convert 成 RGB/RGBA 最省心。
    """
    if image_format == "JPEG":
        # JPEG 不支持 alpha；灰度图保持 L，避免无谓地涨三倍体积。
        return "L" if source.mode == "L" else "RGB"
    if source.mode in {"RGB", "RGBA", "L", "LA"}:
        return source.mode
    has_alpha = source.mode in {"P", "PA"} and "transparency" in source.info
    return "RGBA" if has_alpha or source.mode.endswith("A") else "RGB"


def _has_alpha(source: Image.Image) -> bool:
    """判断图像是否确实携带透明通道，供 Create 选择无损 PNG 输出。"""

    return source.mode in {"RGBA", "LA", "PA"} or (
        source.mode == "P" and "transparency" in source.info
    )


def _normalize_image(
    raw: bytes, *, allowed_formats: frozenset[str], standardize_output: bool
) -> NormalizedImage:
    """按调用方白名单解码、纠正方向、剥离元数据并重编码。"""

    if not raw:
        raise MediaDecodeFailedError("empty image payload")
    try:
        with Image.open(BytesIO(raw)) as probe:
            image_format = str(probe.format or "").upper()
            if image_format not in allowed_formats:
                raise MediaKindUnsupportedError(f"unsupported image format: {image_format or 'unknown'}")
            width, height = probe.size
            if width * height > MAX_IMAGE_PIXELS:
                raise MediaTooLargeError(f"image has too many pixels: {width}x{height}")
            probe.load()  # 触发真实解码：截断文件在这里暴露，而不是落盘之后
            icc_profile = probe.info.get("icc_profile")
            oriented = ImageOps.exif_transpose(probe) or probe
            output_format = (
                "PNG" if _has_alpha(oriented) else "JPEG"
            ) if standardize_output else image_format
            target_mode = _target_mode(output_format, oriented)
            normalized = (
                oriented if oriented.mode == target_mode else oriented.convert(target_mode)
            )
            # convert()/exif_transpose() 都会把 info 复制过来，所以必须再倒一次干净画布：
            # Image.new 的 info 是空的，paste 只搬像素 → 元数据在这一步彻底消失。
            clean = Image.new(target_mode, normalized.size)
            clean.paste(normalized)
    except (MediaError, KeyboardInterrupt):
        raise
    except Exception as err:  # Pillow 的失败面很宽（UnidentifiedImage/OSError/DecompressionBomb…）
        logger.info("media_image_decode_failed bytes=%s error_type=%s", len(raw), type(err).__name__)
        raise MediaDecodeFailedError("failed to decode image") from err

    buffer = BytesIO()
    save_kwargs = {"icc_profile": icc_profile} if icc_profile else {}
    if output_format == "JPEG":
        clean.save(buffer, format="JPEG", quality=88, progressive=True, optimize=True, **save_kwargs)
    else:
        clean.save(buffer, format="PNG", optimize=True, **save_kwargs)
    return NormalizedImage(
        data=buffer.getvalue(),
        mime=_IMAGE_MIME_BY_FORMAT[output_format],
        width=clean.width,
        height=clean.height,
    )


def strip_image_metadata(raw: bytes) -> NormalizedImage:
    """按共享 JPEG/PNG 合同重编码图片并剥离全部元数据（D-4）。

    先按 EXIF Orientation 物理旋转像素，再丢元数据——否则竖拍照片剥完 EXIF 就会躺倒。
    解码失败（含改后缀伪装）抛 ``MediaDecodeFailedError``，非白名单格式抛
    ``MediaKindUnsupportedError``，像素数超上限抛 ``MediaTooLargeError``。
    """

    return _normalize_image(
        raw, allowed_formats=_BASE_IMAGE_FORMATS, standardize_output=False
    )


def normalize_creator_portrait(raw: bytes) -> NormalizedImage:
    """标准化 Plum Create 立绘：接受 JPEG/PNG/WebP/HEIC/HEIF，输出 JPEG/PNG。

    不透明图片统一为 JPEG；有透明通道的 PNG/WebP 统一为 PNG。HEIC/HEIF 原始容器不会
    作为消费者资源落盘，浏览器始终读取标准化结果。
    """

    return _normalize_image(
        raw,
        allowed_formats=_CREATOR_PORTRAIT_FORMATS,
        standardize_output=True,
    )


def _sniff_voice_mime(raw: bytes) -> Optional[str]:
    """用魔数认语音容器；客户端常把录音报成 application/octet-stream。"""
    if len(raw) < 12:
        return None
    if raw[4:8] == b"ftyp":
        return "audio/m4a"
    if raw[:3] == b"ID3" or (raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    if raw[:4] == b"OggS":
        return "audio/ogg"
    if raw[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None


def normalize_voice(
    *, raw: bytes, content_type: str, duration_ms: Optional[int] = None
) -> NormalizedVoice:
    """校验语音容器并原样返回字节（不转码）。

    ``mime`` 以魔数为准、声明值只作兜底：库里存的必须是真实容器，否则读接口下发的
    Content-Type 会骗到客户端播放器。
    """
    if not raw:
        raise MediaDecodeFailedError("empty voice payload")
    declared = str(content_type or "").split(";")[0].strip().lower()
    sniffed = _sniff_voice_mime(raw)
    mime = sniffed or declared
    if mime not in _VOICE_MIME_WHITELIST:
        raise MediaKindUnsupportedError(f"unsupported voice format: {mime or 'unknown'}")
    return NormalizedVoice(data=raw, mime=mime, duration_ms=duration_ms)


def voice_extension(mime: str) -> str:
    """语音容器对应的扩展名，仅用于回给 provider 的临时文件名。"""
    return _VOICE_MIME_WHITELIST.get(mime, ".m4a")


def storage_root() -> Path:
    """媒体落盘根目录（``settings.media_storage_dir``）。"""
    return Path(str(settings.media_storage_dir or "data/media")).expanduser()


def resolve_media_file(storage_path: str) -> Path:
    """把库里的相对路径解析成绝对路径，并挡住路径穿越。

    路径本来只由 :func:`build_storage_path` 生成，这里仍然校验一次：库里一旦被写脏，
    读接口就是任意文件读取。
    """
    cleaned = str(storage_path or "").strip().lstrip("/")
    if not cleaned:
        raise ValueError("empty storage_path")
    root = storage_root().resolve()
    candidate = (root / cleaned).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("storage_path escapes media_storage_dir")
    return candidate


def write_media_file(*, storage_path: str, data: bytes) -> Path:
    """原子落盘（同目录临时文件 + rename），文件 0600、目录 0700。

    先写文件再写库：反过来会出现"库里有行、磁盘没文件"的坏读；孤儿文件由回收 job 兜。
    """
    target = resolve_media_file(storage_path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(target))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def read_media_file(storage_path: str) -> bytes:
    """读回落盘内容；文件缺失抛 ``FileNotFoundError`` 由调用方映射错误码。"""
    return resolve_media_file(storage_path).read_bytes()


def delete_media_file(storage_path: str) -> bool:
    """删除落盘文件，幂等；返回是否真的删掉了。

    分片空目录刻意不清理：两级十六进制目录最多 65536 个，留着比并发删更安全。
    """
    try:
        target = resolve_media_file(storage_path)
    except ValueError:
        logger.warning("media_delete_skipped_invalid_path path=%r", storage_path)
        return False
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


__all__ = [
    "MAX_IMAGE_PIXELS",
    "MEDIA_KIND_IMAGE",
    "MEDIA_KIND_VOICE",
    "MediaDecodeFailedError",
    "MediaError",
    "MediaKindUnsupportedError",
    "MediaTooLargeError",
    "NormalizedImage",
    "NormalizedVoice",
    "build_storage_path",
    "delete_media_file",
    "new_media_id",
    "normalize_creator_portrait",
    "normalize_voice",
    "read_media_file",
    "resolve_media_file",
    "sha256_hex",
    "storage_root",
    "strip_image_metadata",
    "voice_extension",
    "write_media_file",
]
