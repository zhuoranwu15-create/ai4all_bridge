"""SEC-4 鉴权加固回归：空 token 永不通过 + 恒定时间比较。

bearer_matches 是纯函数（无 DB），直接单测其逻辑；再补一条 verify_bridge_auth
的接线检查（只读 settings.ai4all_bridge_secret，不触 DB，用 SimpleNamespace 替身）。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.platform.auth.tokens import bearer_matches
from app.routers import deps


def test_bearer_matches_rejects_empty_or_blank_configured_token():
    # 空 / None / 纯空白配置 token：发 "Bearer " 也不能通过（曾可绕过 bridge/admin）。
    assert bearer_matches("", "Bearer ") is False
    assert bearer_matches(None, "Bearer ") is False
    assert bearer_matches("   ", "Bearer ") is False
    assert bearer_matches("", None) is False


def test_bearer_matches_accepts_exact_token():
    assert bearer_matches("s3cret", "Bearer s3cret") is True
    # 配置侧首尾空白被 strip，但请求侧必须精确匹配 "Bearer <token>"。
    assert bearer_matches("  s3cret  ", "Bearer s3cret") is True


def test_bearer_matches_rejects_wrong_or_malformed():
    assert bearer_matches("s3cret", "Bearer nope") is False
    assert bearer_matches("s3cret", "s3cret") is False          # 缺 Bearer 前缀
    assert bearer_matches("s3cret", "bearer s3cret") is False    # 大小写敏感
    assert bearer_matches("s3cret", None) is False


def test_verify_bridge_auth_rejects_empty_secret(monkeypatch):
    monkeypatch.setattr(deps, "settings", SimpleNamespace(ai4all_bridge_secret=""))
    with pytest.raises(HTTPException) as err:
        deps.verify_bridge_auth(authorization="Bearer ")
    assert err.value.status_code == 401


def test_verify_bridge_auth_accepts_configured_secret(monkeypatch):
    monkeypatch.setattr(deps, "settings", SimpleNamespace(ai4all_bridge_secret="real-secret"))
    # 合法 token 仍放行（返回 None，不抛）。
    assert deps.verify_bridge_auth(authorization="Bearer real-secret") is None
    with pytest.raises(HTTPException):
        deps.verify_bridge_auth(authorization="Bearer wrong")
