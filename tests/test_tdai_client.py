"""TDAI client 单元测试。

覆盖纯函数门控逻辑，不依赖真实 gateway：
- _auth_header：空 key 不发 Authorization（回归点：空 key 时 httpx 会抛 Illegal header value）
- recall：tdai_enabled / tdai_recall_enabled / allowlist 三道门控
- tdai_session_key：稳定格式契约 ai4all:{account_id}
"""
import pytest

from app import tdai_client


def test_session_key_format():
    assert tdai_client.tdai_session_key("aid_806382741") == "ai4all:aid_806382741"
    # 完整 channel 形态 account_id 同样按原样拼接，不做转义/截断
    assert (
        tdai_client.tdai_session_key("openclaw-weixin:local:aid_806382741")
        == "ai4all:openclaw-weixin:local:aid_806382741"
    )


def test_auth_header_empty_key_omits_authorization(monkeypatch):
    """空 api key 时必须返回空 dict，否则 httpx 抛 Illegal header value b'Bearer '。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_gateway_api_key", "", raising=False)
    assert tdai_client._auth_header() == {}
    # 仅空白也视为未配置
    monkeypatch.setattr(tdai_client.settings, "tdai_gateway_api_key", "   ", raising=False)
    assert tdai_client._auth_header() == {}


def test_auth_header_with_key(monkeypatch):
    monkeypatch.setattr(tdai_client.settings, "tdai_gateway_api_key", "secret-xyz", raising=False)
    assert tdai_client._auth_header() == {"Authorization": "Bearer secret-xyz"}


def test_recall_disabled_returns_empty(monkeypatch):
    """tdai_enabled=False 时 recall 直接返回 {}，不触达网络。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", False, raising=False)

    def _boom(**_kwargs):  # _recall_sync 不应被调用
        raise AssertionError("recall 在 disabled 时不应发起 HTTP 调用")

    monkeypatch.setattr(tdai_client, "_recall_sync", _boom)
    assert tdai_client.recall(account_id="aid_806382741", query="hi") == {}


def test_recall_account_not_in_allowlist(monkeypatch):
    """账号不在 allowlist 时 recall 返回 {}，不触达网络。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", True, raising=False)
    monkeypatch.setattr(tdai_client.settings, "tdai_recall_enabled", True, raising=False)
    monkeypatch.setattr(
        tdai_client.settings, "tdai_recall_account_allowlist", "aid_OTHER", raising=False
    )

    def _boom(**_kwargs):
        raise AssertionError("allowlist 外账号不应发起 HTTP 调用")

    monkeypatch.setattr(tdai_client, "_recall_sync", _boom)
    assert tdai_client.recall(account_id="aid_806382741", query="hi") == {}


def test_recall_allowlist_hit_invokes_sync(monkeypatch):
    """命中 allowlist（逗号分隔多账号）时调用 _recall_sync 并透传其返回。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", True, raising=False)
    monkeypatch.setattr(tdai_client.settings, "tdai_recall_enabled", True, raising=False)
    monkeypatch.setattr(
        tdai_client.settings,
        "tdai_recall_account_allowlist",
        "aid_806382741, openclaw-weixin:local:aid_806382741",
        raising=False,
    )
    called = {}

    def _fake_sync(*, account_id, query):
        called["account_id"] = account_id
        return {"prepend_context": "mem", "context": "", "memory_count": 1}

    monkeypatch.setattr(tdai_client, "_recall_sync", _fake_sync)
    out = tdai_client.recall(
        account_id="openclaw-weixin:local:aid_806382741", query="吉他"
    )
    assert out["prepend_context"] == "mem"
    assert called["account_id"] == "openclaw-weixin:local:aid_806382741"


def test_recall_empty_allowlist_blocks_all(monkeypatch):
    """空 allowlist 表示 recall 对所有账号关闭。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", True, raising=False)
    monkeypatch.setattr(tdai_client.settings, "tdai_recall_enabled", True, raising=False)
    monkeypatch.setattr(
        tdai_client.settings, "tdai_recall_account_allowlist", "", raising=False
    )
    assert tdai_client._is_recall_allowed("aid_806382741") is False
