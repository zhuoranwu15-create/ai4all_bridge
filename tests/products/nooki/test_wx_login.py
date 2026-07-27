"""Nooki 微信登录：首次绑定建号、幂等重试、静默续期、未绑定拒绝、冲突 409。全部 mock 微信官方接口。"""

from __future__ import annotations

from fastapi import HTTPException, Response
import pytest

import app.db as db
from app.products.nooki.api import auth as auth_module
from app.products.nooki.infrastructure.repositories.wx_identity import (
    find_platform_user_id_by_openid,
)
from app.products.nooki.infrastructure import wechat_client
from app.products.nooki.infrastructure.wechat_client import WeChatAuthError

# conftest.py 顶部固定的测试用 wx_appid
_WX_APPID = "wx-test-appid"


def _fake_code2session(openid: str, unionid=None):
    def _call(login_code: str):
        return {"openid": openid, "unionid": unionid}

    return _call


def _fake_get_phone(phone_number: str):
    def _call(phone_code: str):
        return {"phone_number": phone_number, "country_code": "86"}

    return _call


def test_bind_creates_platform_user_and_binds_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-1"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900001111"))

    result = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-code-1", phone_code="pc-1"),
        Response(),
    )

    assert result["status"] == "ok"
    assert result["is_new_user"] is True
    assert result["access_token"]
    assert find_platform_user_id_by_openid(
        wx_appid=_WX_APPID, openid="openid-1"
    ) == result["platform_user_id"]


def test_bind_retry_with_same_openid_is_idempotent(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-2"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900002222"))

    first = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-code-2a", phone_code="pc-2a"),
        Response(),
    )
    second = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-code-2b", phone_code="pc-2b"),
        Response(),
    )

    assert first["platform_user_id"] == second["platform_user_id"]
    assert second["is_new_user"] is False


def test_session_renews_for_bound_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-3"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900003333"))
    bind_result = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-code-3", phone_code="pc-3"),
        Response(),
    )

    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-3"))
    session_result = auth_module.nooki_auth_session(
        auth_module.WxSessionRequest(login_code="js-code-3-renew"), Response()
    )

    assert session_result["status"] == "ok"
    assert session_result["access_token"] != bind_result["access_token"]


def test_session_rejects_unbound_openid(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-never-bound"))

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_session(
            auth_module.WxSessionRequest(login_code="js-code-x"), Response()
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "wx_identity_not_bound"


def test_bind_translates_wechat_error_to_400(fresh_db, monkeypatch):
    def _raise(login_code: str):
        raise WeChatAuthError("wx_code2session_failed", "boom")

    monkeypatch.setattr(auth_module, "code2session", _raise)

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_bind(
            auth_module.WxBindRequest(login_code="bad-code", phone_code="pc"),
            Response(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "wx_code2session_failed"


def test_bind_returns_409_when_openid_and_phone_belong_to_different_users(
    fresh_db, monkeypatch
):
    """openid 已绑定 user_A，但 phone_code 换出的手机号对应另一个 user_B → 409，不改绑。"""

    # 建两个不同用户
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-B"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900002222"))
    auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-B", phone_code="pc-B"), Response()
    )

    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-A"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900001111"))
    result_a = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-A", phone_code="pc-A"), Response()
    )

    # openid-A 已绑定 user_A；这次手机号换成 user_B 的 → 冲突
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-A"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900002222"))
    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_bind(
            auth_module.WxBindRequest(login_code="js-A-conflict", phone_code="pc-B"),
            Response(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "wx_identity_conflict"
    # 绑定未篡改：openid-A 仍指向 user_A
    assert find_platform_user_id_by_openid(
        wx_appid=_WX_APPID, openid="openid-A"
    ) == result_a["platform_user_id"]


def test_bind_same_openid_same_phone_after_conflict_is_idempotent(fresh_db, monkeypatch):
    """openid 已绑定 user_A，重试时手机号仍是 user_A 的 → 幂等，不 409。"""

    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-C"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900003333"))
    first = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-C", phone_code="pc-C"), Response()
    )

    # 同 openid + 同手机号重试 → 幂等
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-C"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900003333"))
    second = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-C-retry", phone_code="pc-C-retry"),
        Response(),
    )

    assert first["platform_user_id"] == second["platform_user_id"]
    assert second["is_new_user"] is False


def test_bind_rejects_bound_openid_with_previously_unknown_phone(fresh_db, monkeypatch):
    """新手机号也会解析成独立用户，不能借已绑定 OpenID 登录旧用户。"""

    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-D"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900004444"))
    first = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-D", phone_code="pc-D"), Response()
    )

    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900005555"))
    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_bind(
            auth_module.WxBindRequest(login_code="js-D-2", phone_code="pc-new"),
            Response(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "wx_identity_conflict"
    assert find_platform_user_id_by_openid(
        wx_appid=_WX_APPID, openid="openid-D"
    ) == first["platform_user_id"]


def test_session_rejects_disabled_membership_with_403(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-E"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900006666"))
    bound = auth_module.nooki_auth_bind(
        auth_module.WxBindRequest(login_code="js-E", phone_code="pc-E"), Response()
    )
    db.update_product_membership_status(
        platform_user_id=bound["platform_user_id"], app_id="nooki", status="disabled"
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_session(
            auth_module.WxSessionRequest(login_code="js-E-renew"), Response()
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "product_membership_disabled"


def test_bind_rechecks_identity_owner_after_concurrent_insert(fresh_db, monkeypatch):
    monkeypatch.setattr(auth_module, "code2session", _fake_code2session("openid-race"))
    monkeypatch.setattr(auth_module, "get_phone_number", _fake_get_phone("13900007777"))
    monkeypatch.setattr(
        auth_module,
        "bind_wx_identity",
        lambda **kwargs: "another-platform-user",
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_module.nooki_auth_bind(
            auth_module.WxBindRequest(login_code="js-race", phone_code="pc-race"),
            Response(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "wx_identity_conflict"


def test_code2session_does_not_require_or_return_session_key(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"openid": "openid-no-session-key", "unionid": "union-1"}

    monkeypatch.setattr(wechat_client.httpx, "get", lambda *args, **kwargs: _Response())

    result = wechat_client.code2session("login-code")

    assert result == {"openid": "openid-no-session-key", "unionid": "union-1"}
