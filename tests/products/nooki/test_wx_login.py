"""Nooki 微信登录：首次绑定建号、幂等重试、静默续期、未绑定拒绝。全部 mock 微信官方接口。"""

from __future__ import annotations

from fastapi import HTTPException, Response
import pytest

from app.products.nooki.api import auth as auth_module
from app.products.nooki.infrastructure.repositories.wx_identity import (
    find_platform_user_id_by_openid,
)
from app.products.nooki.infrastructure.wechat_client import WeChatAuthError


def _fake_code2session(openid: str, session_key: str = "sess-key", unionid=None):
    def _call(js_code: str):
        return {"openid": openid, "session_key": session_key, "unionid": unionid}

    return _call


def _fake_decrypt(phone_number: str):
    def _call(*, encrypted_data: str, iv: str, session_key: str):
        return {"phone_number": phone_number, "country_code": "86"}

    return _call


def test_bind_creates_platform_user_and_binds_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-1"))
    monkeypatch.setattr(auth_module, "decrypt_phone_number", _fake_decrypt("13900001111"))

    result = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(code="js-code-1", encrypted_data="enc", iv="iv"),
        Response(),
    )

    assert result["status"] == "ok"
    assert result["is_new_user"] is True
    assert result["access_token"]
    assert find_platform_user_id_by_openid(app_id="nooki", openid="openid-1") == (
        result["platform_user_id"]
    )


def test_bind_retry_with_same_openid_is_idempotent(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-2"))
    monkeypatch.setattr(auth_module, "decrypt_phone_number", _fake_decrypt("13900002222"))

    first = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(code="js-code-2a", encrypted_data="enc", iv="iv"),
        Response(),
    )
    second = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(code="js-code-2b", encrypted_data="enc", iv="iv"),
        Response(),
    )

    assert first["platform_user_id"] == second["platform_user_id"]
    assert second["is_new_user"] is False


def test_session_renews_for_bound_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-3"))
    monkeypatch.setattr(auth_module, "decrypt_phone_number", _fake_decrypt("13900003333"))
    bind_result = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(code="js-code-3", encrypted_data="enc", iv="iv"),
        Response(),
    )

    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-3"))
    session_result = auth_module.nooki_auth_session(
        auth_module.WxSessionRequest(code="js-code-3-renew"), Response()
    )

    assert session_result["status"] == "ok"
    assert session_result["access_token"] != bind_result["access_token"]


def test_session_rejects_unbound_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-never-bound"))

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_session(auth_module.WxSessionRequest(code="js-code-x"), Response())

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "wx_identity_not_bound"


def test_bind_translates_wechat_error_to_400(fresh_db, monkeypatch):
    def _raise(js_code: str):
        raise WeChatAuthError("wx_code2session_failed", "boom")

    monkeypatch.setattr(auth_module, "code2session", _raise)

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_bind(
            auth_module.WxBindRequest(code="bad-code", encrypted_data="enc", iv="iv"), Response()
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "wx_code2session_failed"
