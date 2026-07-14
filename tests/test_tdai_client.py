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


def test_recall_volume_eligible_bypasses_allowlist(monkeypatch):
    """不在 allowlist 但 volume_eligible=True（消息数越阈值）时仍放行 recall。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", True, raising=False)
    monkeypatch.setattr(tdai_client.settings, "tdai_recall_enabled", True, raising=False)
    monkeypatch.setattr(
        tdai_client.settings, "tdai_recall_account_allowlist", "", raising=False
    )
    called = {}

    def _fake_sync(*, account_id, query):
        called["account_id"] = account_id
        return {"prepend_context": "mem", "context": "", "memory_count": 1}

    monkeypatch.setattr(tdai_client, "_recall_sync", _fake_sync)
    # allowlist 空 → 无 volume_eligible 时被拦
    assert tdai_client.recall(account_id="aid_x", query="q") == {}
    # volume_eligible=True → 放行
    out = tdai_client.recall(account_id="aid_x", query="q", volume_eligible=True)
    assert out["prepend_context"] == "mem"
    assert called["account_id"] == "aid_x"


def test_recall_volume_eligible_still_blocked_by_master_switch(monkeypatch):
    """volume_eligible 不能越过总开关 / recall 子开关。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", False, raising=False)

    def _boom(**_kwargs):
        raise AssertionError("_recall_sync must not be called when master switch off")

    monkeypatch.setattr(tdai_client, "_recall_sync", _boom)
    assert tdai_client.recall(account_id="aid_x", query="q", volume_eligible=True) == {}


# ---------------------------------------------------------------------------
# 主动检索工具 gating / 封装 / 多租户探针
# ---------------------------------------------------------------------------

def _enable_search(monkeypatch, *, allowlist="aid_806382741", mt_unsafe=False):
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", True, raising=False)
    monkeypatch.setattr(tdai_client.settings, "tdai_search_enabled", True, raising=False)
    monkeypatch.setattr(
        tdai_client.settings, "tdai_search_account_allowlist", allowlist, raising=False
    )
    monkeypatch.setattr(tdai_client, "_MT_UNSAFE", mt_unsafe, raising=False)


def test_search_allowed_three_gates(monkeypatch):
    """search_allowed：总开关 + search 开关 + allowlist + 多租户闸门，缺一即 False。"""
    _enable_search(monkeypatch)
    assert tdai_client.search_allowed("aid_806382741") is True
    # 账号不在 allowlist
    assert tdai_client.search_allowed("aid_OTHER") is False
    # 空 allowlist = 全关
    _enable_search(monkeypatch, allowlist="")
    assert tdai_client.search_allowed("aid_806382741") is False
    # search 子开关关
    _enable_search(monkeypatch)
    monkeypatch.setattr(tdai_client.settings, "tdai_search_enabled", False, raising=False)
    assert tdai_client.search_allowed("aid_806382741") is False
    # 总开关关
    _enable_search(monkeypatch)
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", False, raising=False)
    assert tdai_client.search_allowed("aid_806382741") is False


def test_search_allowed_blocked_when_mt_unsafe(monkeypatch):
    """多租户安全闸门置位时 search 一律关闭（即便 allowlist 命中 / volume_eligible）。"""
    _enable_search(monkeypatch, mt_unsafe=True)
    assert tdai_client.search_allowed("aid_806382741") is False
    assert tdai_client.search_allowed("aid_806382741", volume_eligible=True) is False


def test_search_allowed_volume_eligible_bypasses_allowlist(monkeypatch):
    """空 allowlist 但 volume_eligible=True 时 search 放行；总开关/子开关仍优先。"""
    _enable_search(monkeypatch, allowlist="")
    # 不在 allowlist 且无 volume_eligible → 关
    assert tdai_client.search_allowed("aid_x") is False
    # volume_eligible=True → 开
    assert tdai_client.search_allowed("aid_x", volume_eligible=True) is True
    # 但总开关关时 volume_eligible 无效
    monkeypatch.setattr(tdai_client.settings, "tdai_enabled", False, raising=False)
    assert tdai_client.search_allowed("aid_x", volume_eligible=True) is False


def test_search_memories_injects_session_key(monkeypatch):
    """search_memories 强制注入 session_key=ai4all:{account_id}，并透传响应 json。"""
    captured = {}

    def _fake_sync(endpoint, *, account_id, payload):
        captured["endpoint"] = endpoint
        captured["account_id"] = account_id
        captured["payload"] = payload
        return {"results": "mem text", "total": 2, "strategy": "hybrid"}

    monkeypatch.setattr(tdai_client, "_search_sync", _fake_sync)
    out = tdai_client.search_memories(
        account_id="aid_806382741", query="吉他偏好", limit=5, type="persona"
    )
    assert out["results"] == "mem text" and out["total"] == 2
    assert captured["endpoint"] == "/search/memories"
    assert captured["payload"] == {"query": "吉他偏好", "limit": 5, "type": "persona"}
    # session_key 由 _search_sync 内部注入（此处 fake 未注入，契约在 _search_sync 层验证）


def test_search_sync_injects_session_key_and_degrades(monkeypatch):
    """_search_sync：注入 session_key；超时/异常返回 {} never-raise。"""
    monkeypatch.setattr(tdai_client.settings, "tdai_gateway_url", "http://x", raising=False)
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": "ok", "total": 1}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            seen["json"] = json
            return _Resp()

    monkeypatch.setattr(tdai_client.httpx, "Client", _Client)
    out = tdai_client._search_sync(
        "/search/conversations", account_id="aid_806382741", payload={"query": "q", "limit": 5}
    )
    assert out == {"results": "ok", "total": 1}
    assert seen["json"]["session_key"] == "ai4all:aid_806382741"

    # 异常降级为 {}
    class _BoomClient(_Client):
        def post(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(tdai_client.httpx, "Client", _BoomClient)
    assert tdai_client._search_sync(
        "/search/memories", account_id="aid_806382741", payload={"query": "q", "limit": 5}
    ) == {}


def _patch_probe_status(monkeypatch, status_code=None, raise_exc=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(tdai_client.httpx, "Client", _Client)


def test_verify_multitenant_400_is_safe(monkeypatch):
    """缺 session_key 返 400 → 强隔离模式，安全。"""
    _patch_probe_status(monkeypatch, status_code=400)
    assert tdai_client.verify_multitenant() is True


def test_verify_multitenant_200_is_unsafe(monkeypatch):
    """缺 session_key 返 200 → 共享库模式，不安全。"""
    _patch_probe_status(monkeypatch, status_code=200)
    assert tdai_client.verify_multitenant() is False


def test_verify_multitenant_network_error_returns_none(monkeypatch):
    """网络错误 → None，best-effort 不阻塞启动。"""
    _patch_probe_status(monkeypatch, raise_exc=RuntimeError("conn refused"))
    assert tdai_client.verify_multitenant() is None


def test_mark_multitenant_unsafe_toggles_gate(monkeypatch):
    _enable_search(monkeypatch)
    assert tdai_client.search_allowed("aid_806382741") is True
    tdai_client.mark_multitenant_unsafe()
    try:
        assert tdai_client.search_allowed("aid_806382741") is False
    finally:
        tdai_client._MT_UNSAFE = False  # 复位模块级全局，避免污染其它用例
