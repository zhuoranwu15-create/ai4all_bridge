"""Provider-neutral batch speech-to-text service for application channels."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings


logger = logging.getLogger("ai4all.asr")


class ASRError(RuntimeError):
    """Base exception safe for the App API to map to a stable error code."""


class ASRNotConfiguredError(ASRError):
    pass


class ASRProviderError(ASRError):
    pass


def _local_mock_transcript() -> Optional[str]:
    if str(settings.app_env or "").lower() not in {"local", "development", "test"}:
        return None
    text = str(getattr(settings, "asr_mock_transcript", "") or "").strip()
    return text or None


def is_asr_available() -> bool:
    return bool(_local_mock_transcript() or str(settings.asr_api_key or "").strip())


def transcribe_audio(
    *, content: bytes, filename: str, content_type: str, language: str = "zh"
) -> str:
    """Transcribe in-memory audio without persisting the original recording."""
    mock = _local_mock_transcript()
    if mock:
        return mock

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
