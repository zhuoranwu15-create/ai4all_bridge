"""Provider-neutral batch speech-to-text service for application channels."""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings


logger = logging.getLogger("ai4all.asr")

_OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"
_VOLCENGINE_FLASH_PROVIDER = "volcengine_flash"
_VOLCENGINE_SUCCESS_CODE = "20000000"


class ASRError(RuntimeError):
    """Base exception safe for the App API to map to a stable error code."""


class ASRNotConfiguredError(ASRError):
    """The selected ASR provider is missing required deployment configuration."""


class ASRProviderError(ASRError):
    """The selected ASR provider or its local audio preparation failed."""


def _local_mock_transcript() -> Optional[str]:
    if str(settings.app_env or "").lower() not in {"local", "development", "test"}:
        return None
    text = str(getattr(settings, "asr_mock_transcript", "") or "").strip()
    return text or None


def _provider() -> str:
    value = getattr(settings, "asr_provider", _OPENAI_COMPATIBLE_PROVIDER)
    return str(value or "").strip().lower()


def _volcengine_credentials() -> tuple[str, str, str]:
    """Return API key or legacy App ID/token without ever logging their values."""
    return (
        str(getattr(settings, "volcengine_asr_api_key", "") or "").strip(),
        str(getattr(settings, "volcengine_asr_app_id", "") or "").strip(),
        str(getattr(settings, "volcengine_asr_access_token", "") or "").strip(),
    )


def is_asr_available() -> bool:
    """Whether the selected provider has enough credentials to accept requests."""
    if _local_mock_transcript():
        return True
    if _provider() == _VOLCENGINE_FLASH_PROVIDER:
        api_key, app_id, access_token = _volcengine_credentials()
        return bool(api_key or (app_id and access_token))
    if _provider() == _OPENAI_COMPATIBLE_PROVIDER:
        return bool(str(settings.asr_api_key or "").strip())
    return False


def _transcode_to_wav(*, content: bytes, filename: str) -> bytes:
    """Use ffmpeg to create an ephemeral 16kHz mono PCM WAV accepted by turbo ASR."""
    ffmpeg = str(getattr(settings, "asr_ffmpeg_path", "") or "").strip()
    if not ffmpeg:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as err:
            logger.warning("asr_ffmpeg_unavailable error_type=%s", type(err).__name__)
            raise ASRProviderError("ASR audio transcoder is not available") from err
    safe_suffix = Path(filename or "recording.m4a").suffix.lower()
    if not safe_suffix or len(safe_suffix) > 10 or not safe_suffix[1:].isalnum():
        safe_suffix = ".audio"
    max_seconds = max(1.0, float(settings.asr_max_duration_ms) / 1000.0)
    timeout = max(1.0, float(getattr(settings, "asr_transcode_timeout_seconds", 15.0)))

    try:
        with tempfile.TemporaryDirectory(prefix="ai4all-asr-") as tmp_dir:
            input_path = Path(tmp_dir) / f"input{safe_suffix}"
            output_path = Path(tmp_dir) / "output.wav"
            input_path.write_bytes(content)
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(input_path),
                    "-map",
                    "0:a:0",
                    "-t",
                    f"{max_seconds:g}",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(output_path),
                ],
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            if completed.returncode != 0 or not output_path.is_file():
                logger.warning(
                    "asr_transcode_rejected returncode=%s", completed.returncode
                )
                raise ASRProviderError("ASR audio transcoding failed")
            wav = output_path.read_bytes()
    except ASRProviderError:
        raise
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as err:
        logger.warning("asr_transcode_failed error_type=%s", type(err).__name__)
        raise ASRProviderError("ASR audio transcoding failed") from err

    if not wav or len(wav) > int(settings.asr_max_audio_bytes):
        raise ASRProviderError("ASR transcoded audio exceeds the configured limit")
    return wav


def _volcengine_headers(*, request_id: str) -> dict[str, str]:
    """Build one of Volcengine's documented authentication header variants."""
    api_key, app_id, access_token = _volcengine_credentials()
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": str(settings.volcengine_asr_resource_id or "").strip(),
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    elif app_id and access_token:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token
    else:
        raise ASRNotConfiguredError("Volcengine ASR credentials are not configured")
    if not headers["X-Api-Resource-Id"]:
        raise ASRNotConfiguredError("Volcengine ASR resource ID is not configured")
    return headers


def _transcript_from_volcengine(payload: Any) -> str:
    """Extract the full text while tolerating the documented result variants."""
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        transcript = str(result.get("text") or "").strip()
        if transcript:
            return transcript
        utterances = result.get("utterances")
        if isinstance(utterances, list):
            return "".join(
                str(item.get("text") or "").strip()
                for item in utterances
                if isinstance(item, dict)
            ).strip()
    return str(payload.get("text") or payload.get("transcript") or "").strip()


def _transcribe_volcengine_flash(*, content: bytes, filename: str) -> str:
    endpoint = str(settings.volcengine_asr_endpoint or "").strip()
    if not endpoint:
        raise ASRNotConfiguredError("Volcengine ASR endpoint is not configured")
    wav = _transcode_to_wav(content=content, filename=filename)
    request_id = str(uuid.uuid4())
    body = {
        "user": {"uid": "ai4all-zhaoxi"},
        "audio": {"format": "wav", "data": base64.b64encode(wav).decode("ascii")},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
            "result_type": "full",
        },
    }
    response: Optional[httpx.Response] = None
    try:
        with httpx.Client(timeout=float(settings.asr_timeout_seconds)) as client:
            response = client.post(
                endpoint,
                headers=_volcengine_headers(request_id=request_id),
                json=body,
            )
        response.raise_for_status()
        status_code = str(response.headers.get("X-Api-Status-Code") or "").strip()
        if status_code and status_code != _VOLCENGINE_SUCCESS_CODE:
            raise ASRProviderError("Volcengine ASR rejected the request")
        payload = response.json()
    except ASRNotConfiguredError:
        raise
    except ASRProviderError:
        logger.warning(
            "asr_volcengine_rejected request_id=%s vendor_code=%s log_id=%s",
            request_id,
            str(response.headers.get("X-Api-Status-Code") or "")
            if response is not None
            else "",
            str(response.headers.get("X-Tt-Logid") or "")
            if response is not None
            else "",
        )
        raise
    except (httpx.HTTPError, ValueError) as err:
        logger.warning(
            "asr_volcengine_failed request_id=%s error_type=%s vendor_code=%s log_id=%s",
            request_id,
            type(err).__name__,
            str(response.headers.get("X-Api-Status-Code") or "")
            if response is not None
            else "",
            str(response.headers.get("X-Tt-Logid") or "")
            if response is not None
            else "",
        )
        raise ASRProviderError("Volcengine ASR request failed") from err

    transcript = _transcript_from_volcengine(payload)
    if not transcript:
        logger.warning(
            "asr_volcengine_empty request_id=%s log_id=%s",
            request_id,
            str(response.headers.get("X-Tt-Logid") or ""),
        )
        raise ASRProviderError("Volcengine ASR returned an empty transcript")
    logger.info(
        "asr_volcengine_succeeded request_id=%s input_bytes=%s wav_bytes=%s log_id=%s",
        request_id,
        len(content),
        len(wav),
        str(response.headers.get("X-Tt-Logid") or ""),
    )
    return transcript


def _transcribe_openai_compatible(
    *, content: bytes, filename: str, content_type: str, language: str
) -> str:
    api_key = str(settings.asr_api_key or "").strip()
    if not api_key:
        raise ASRNotConfiguredError("ASR provider is not configured")
    base_url = str(settings.asr_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ASRNotConfiguredError("ASR base URL is not configured")
    endpoint = (
        base_url
        if base_url.endswith("/audio/transcriptions")
        else f"{base_url}/audio/transcriptions"
    )
    safe_name = Path(filename or "recording.m4a").name or "recording.m4a"
    try:
        with httpx.Client(timeout=float(settings.asr_timeout_seconds)) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (safe_name, content, content_type)},
                data={
                    "model": str(settings.asr_model or "whisper-1"),
                    "language": language,
                    "response_format": "json",
                },
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as err:
        logger.warning(
            "asr_provider_failed bytes=%s content_type=%s error_type=%s",
            len(content),
            content_type,
            type(err).__name__,
        )
        raise ASRProviderError("ASR provider request failed") from err

    transcript = str(payload.get("text") or payload.get("transcript") or "").strip()
    if not transcript:
        raise ASRProviderError("ASR provider returned an empty transcript")
    return transcript


def transcribe_audio(
    *, content: bytes, filename: str, content_type: str, language: str = "zh"
) -> str:
    """Transcribe audio without changing or durably storing the original media."""
    mock = _local_mock_transcript()
    if mock:
        return mock

    provider = _provider()
    if provider == _VOLCENGINE_FLASH_PROVIDER:
        return _transcribe_volcengine_flash(
            content=content,
            filename=filename,
        )
    if provider == _OPENAI_COMPATIBLE_PROVIDER:
        return _transcribe_openai_compatible(
            content=content,
            filename=filename,
            content_type=content_type,
            language=language,
        )
    raise ASRNotConfiguredError("Unknown ASR provider")
