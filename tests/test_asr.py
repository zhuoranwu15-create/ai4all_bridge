"""Provider and M4A preparation tests for batch ASR."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import httpx
import pytest

from app.platform.media import asr


class _Client:
    def __init__(self, response: httpx.Response, captured: dict):
        self.response = response
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, endpoint, **kwargs):
        self.captured.update(endpoint=endpoint, **kwargs)
        return self.response


def _volcengine_settings(test_settings) -> None:
    test_settings.asr_provider = "volcengine_flash"
    test_settings.volcengine_asr_endpoint = "https://openspeech.example/asr"
    test_settings.volcengine_asr_resource_id = "volc.bigasr.auc_turbo"
    test_settings.volcengine_asr_app_id = "app-id"
    test_settings.volcengine_asr_access_token = "access-token"
    test_settings.volcengine_asr_api_key = ""


def test_volcengine_flash_transcribes_m4a_with_legacy_credentials(
    monkeypatch, test_settings
):
    _volcengine_settings(test_settings)
    monkeypatch.setattr(asr, "settings", test_settings)
    monkeypatch.setattr(asr, "_transcode_to_wav", lambda **_kwargs: b"RIFF-wav")
    captured = {}
    response = httpx.Response(
        200,
        headers={"X-Api-Status-Code": "20000000", "X-Tt-Logid": "log-safe"},
        json={"result": {"text": "今天心情不错。"}},
        request=httpx.Request("POST", "https://openspeech.example/asr"),
    )
    monkeypatch.setattr(
        asr.httpx,
        "Client",
        lambda **_kwargs: _Client(response, captured),
    )

    transcript = asr.transcribe_audio(
        content=b"m4a-bytes",
        filename="clip.m4a",
        content_type="audio/m4a",
    )

    assert transcript == "今天心情不错。"
    assert captured["endpoint"] == test_settings.volcengine_asr_endpoint
    assert captured["headers"]["X-Api-App-Key"] == "app-id"
    assert captured["headers"]["X-Api-Access-Key"] == "access-token"
    assert captured["headers"]["X-Api-Resource-Id"] == "volc.bigasr.auc_turbo"
    assert captured["headers"]["X-Api-Sequence"] == "-1"
    assert captured["json"]["audio"] == {
        "format": "wav",
        "data": base64.b64encode(b"RIFF-wav").decode("ascii"),
    }


def test_volcengine_flash_prefers_new_api_key(monkeypatch, test_settings):
    _volcengine_settings(test_settings)
    test_settings.volcengine_asr_api_key = "new-api-key"
    monkeypatch.setattr(asr, "settings", test_settings)

    headers = asr._volcengine_headers(request_id="request-id")

    assert headers["X-Api-Key"] == "new-api-key"
    assert "X-Api-App-Key" not in headers
    assert "X-Api-Access-Key" not in headers


def test_volcengine_flash_rejects_vendor_error_code(monkeypatch, test_settings):
    _volcengine_settings(test_settings)
    monkeypatch.setattr(asr, "settings", test_settings)
    monkeypatch.setattr(asr, "_transcode_to_wav", lambda **_kwargs: b"RIFF-wav")
    response = httpx.Response(
        200,
        headers={"X-Api-Status-Code": "45000080", "X-Tt-Logid": "log-safe"},
        json={"result": {}},
        request=httpx.Request("POST", "https://openspeech.example/asr"),
    )
    monkeypatch.setattr(
        asr.httpx,
        "Client",
        lambda **_kwargs: _Client(response, {}),
    )

    with pytest.raises(asr.ASRProviderError):
        asr.transcribe_audio(
            content=b"m4a-bytes",
            filename="clip.m4a",
            content_type="audio/m4a",
        )


def test_transcode_to_wav_uses_bounded_ffmpeg_process(monkeypatch, test_settings):
    test_settings.asr_ffmpeg_path = "ffmpeg"
    monkeypatch.setattr(asr, "settings", test_settings)
    captured = {}

    def _run(command, **kwargs):
        captured.update(command=command, **kwargs)
        Path(command[-1]).write_bytes(b"RIFF-valid-wav")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(asr.subprocess, "run", _run)

    wav = asr._transcode_to_wav(content=b"m4a", filename="clip.m4a")

    assert wav == b"RIFF-valid-wav"
    assert captured["command"][0] == "ffmpeg"
    assert captured["command"][captured["command"].index("-ac") + 1] == "1"
    assert captured["command"][captured["command"].index("-ar") + 1] == "16000"
    assert captured["timeout"] == 2.0
    assert captured["check"] is False


def test_is_asr_available_requires_complete_selected_credentials(
    monkeypatch, test_settings
):
    _volcengine_settings(test_settings)
    monkeypatch.setattr(asr, "settings", test_settings)
    assert asr.is_asr_available() is True

    test_settings.volcengine_asr_access_token = ""
    assert asr.is_asr_available() is False
