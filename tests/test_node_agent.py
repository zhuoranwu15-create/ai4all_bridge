"""Phase 3:节点 agent(exec 端点 + 出站 pull)、远程 logout 误判修复、出站派发分支。

- exec 端点鉴权 + logout 原始错误文案穿透 HTTP body;
- 远程 logout "does not support logout" 经中心 _cleanup 分类为 unsupported(非 failed)——回归锚点;
- run_outbound_pull_once:claim→本机发→回报 sent/failed,中心状态机随动;
- dispatch_proactive_text / 欢迎语 inline vs central 分支。
"""
from datetime import datetime
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.config import Settings
from app.openclaw_gateway import OpenClawGatewayError


_BEARER = {"Authorization": "Bearer test-secret"}


def _raise(exc):
    def _fn(**_kw):
        raise exc
    return _fn


def _seed_outbound(idem, node_id="aliyun2", account_id="acc-node"):
    from app.db import create_outbound_message, get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"session-{account_id}",
    )
    return create_outbound_message(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{account_id}",
        source="reminder",
        text="hi",
        idempotency_key=idem,
        quota_date="2026-06-12",
        node_id=node_id,
    )


# ===== exec 端点:鉴权 + logout 错误文案穿透 =====

def test_exec_logout_requires_bearer_and_surfaces_unsupported(monkeypatch):
    from app import node_agent

    monkeypatch.setattr(
        node_agent, "settings", Settings(ai4all_bridge_secret="test-secret", node_id="aliyun2")
    )
    tc = TestClient(node_agent.create_node_agent_app())

    # 缺 bearer → 401
    assert tc.post("/node/exec/logout", json={"account_id": "a", "timeout_ms": 5000}).status_code == 401
    # health 不需鉴权
    assert tc.get("/health/live").status_code == 200

    # openclaw 不支持 logout → 502 且 body 含原始文案(供中心据此分类)
    monkeypatch.setattr(
        node_agent.openclaw_gateway,
        "logout_weixin_account",
        _raise(OpenClawGatewayError("openclaw does not support logout")),
    )
    r = tc.post("/node/exec/logout", json={"account_id": "a", "timeout_ms": 5000}, headers=_BEARER)
    assert r.status_code == 502
    assert "does not support logout" in r.text


# ===== 远程 logout 误判修复(回归锚点)=====

def test_remote_logout_unsupported_not_misjudged_as_failed(monkeypatch, fresh_db):
    """中心远程 logout 一个 openclaw 不支持 logout 的节点:_exec_post 必须把原始文案
    透出,使 main._cleanup 分类为 'unsupported'(修复前会因 httpx 错误串丢文案被误判 'failed')。"""
    from app import main, node_agent, node_gateway
    from app.db import upsert_access_node

    # 注册远程节点 base_url(fresh_db 已 patch app.db.settings + init_db)
    upsert_access_node(node_id="aliyun2", base_url="http://aliyun2")

    # 节点 agent:openclaw 抛 "does not support logout"
    monkeypatch.setattr(
        node_agent, "settings", Settings(ai4all_bridge_secret="test-secret", node_id="aliyun2")
    )
    monkeypatch.setattr(
        node_agent.openclaw_gateway,
        "logout_weixin_account",
        _raise(OpenClawGatewayError("openclaw does not support logout")),
    )
    agent_client = TestClient(node_agent.create_node_agent_app())

    # node_gateway:central 角色(aliyun2 为远程),secret 对齐;httpx.post 路由到 agent app
    monkeypatch.setattr(
        node_gateway,
        "settings",
        Settings(ai4all_role="central", node_id="aliyun1", ai4all_bridge_secret="test-secret"),
    )

    def _routed_post(url, *, json=None, headers=None, timeout=None):
        # 把中心发往远程节点的绝对 URL 按 path 路由到 agent 的 TestClient(同进程跑 ASGI)。
        return agent_client.post(urlsplit(url).path, json=json, headers=headers)

    monkeypatch.setattr(node_gateway.httpx, "post", _routed_post)

    # main._cleanup 读 settings.openclaw_gateway_call_timeout_ms
    monkeypatch.setattr(main, "settings", Settings(openclaw_gateway_call_timeout_ms=5000))

    bindings = [{"channel": "openclaw-weixin", "channel_account_id": "testacct-im-bot"}]
    out = main._cleanup_openclaw_weixin_accounts(bindings, node_id="aliyun2")
    assert out["status"] == "unsupported"
    assert out["attempts"][0]["status"] == "unsupported"
    assert "does not support logout" in out["attempts"][0]["error"]


# ===== 出站 pull 一轮 =====

def _node_settings():
    return Settings(
        ai4all_role="node",
        node_id="aliyun2",
        central_url="http://central",
        ai4all_bridge_secret="test-secret",
        outbound_pull_batch_size=20,
        openclaw_gateway_call_timeout_ms=5000,
    )


def test_node_pull_once_sends_and_marks_sent(client, monkeypatch):
    from app import node_agent
    from app.db import get_outbound_message

    row = _seed_outbound("pull-ok")
    monkeypatch.setattr(node_agent, "settings", _node_settings())
    monkeypatch.setattr(
        node_agent.openclaw_gateway, "send_weixin_text", lambda **kw: {"messageId": "gw-1"}
    )

    summary = node_agent.run_outbound_pull_once(http_client=client)
    assert summary == {"claimed": 1, "sent": 1, "failed": 0}

    got = get_outbound_message(outbound_message_id=row["id"])
    assert got["status"] == "sent" and got["gateway_message_id"] == "gw-1"
    # 已 sent → 再 pull 不重领
    assert node_agent.run_outbound_pull_once(http_client=client)["claimed"] == 0


def test_node_pull_once_reports_failure(client, monkeypatch):
    from app import node_agent
    from app.db import get_outbound_message

    row = _seed_outbound("pull-fail")
    monkeypatch.setattr(node_agent, "settings", _node_settings())
    monkeypatch.setattr(
        node_agent.openclaw_gateway, "send_weixin_text", _raise(RuntimeError("boom"))
    )

    summary = node_agent.run_outbound_pull_once(http_client=client)
    assert summary["claimed"] == 1 and summary["failed"] == 1

    got = get_outbound_message(outbound_message_id=row["id"])
    assert got["status"] == "failed" and "boom" in (got["error"] or "")


# ===== 出站派发分支(inline vs central)=====

def _create_account(account_id, node_id=None):
    from app.db import get_or_create_session, set_account_assigned_node

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"session-{account_id}",
    )
    if node_id is not None:
        set_account_assigned_node(account_id=account_id, node_id=node_id)


def test_dispatch_proactive_central_enqueues_without_send(fresh_db, monkeypatch):
    from app.proactive import messaging

    _create_account("acc-central")
    monkeypatch.setattr(
        messaging, "settings", Settings(ai4all_role="central", default_node_id="aliyun1")
    )
    sent = {}
    monkeypatch.setattr(messaging, "send_weixin_text", lambda **kw: sent.setdefault("called", True))

    out = messaging.dispatch_proactive_text(
        account_id="acc-central",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-central",
        source="account_check",
        text="hi",
        idempotency_key="disp-central",
        now=datetime(2026, 6, 12, 10, 0),
        product_category="companion_followup",
    )
    assert "called" not in sent  # 中心不直发
    assert out["status"] == "pending" and out["node_id"] == "aliyun1"


def test_dispatch_proactive_inline_sends(fresh_db, monkeypatch):
    from app.proactive import messaging

    _create_account("acc-inline")
    monkeypatch.setattr(messaging, "settings", Settings(ai4all_role="standalone"))
    sent = {}
    monkeypatch.setattr(
        messaging,
        "send_weixin_text",
        lambda **kw: sent.setdefault("called", True) or {"messageId": "m-inline"},
    )

    out = messaging.dispatch_proactive_text(
        account_id="acc-inline",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-inline",
        source="account_check",
        text="hi",
        idempotency_key="disp-inline",
        now=datetime(2026, 6, 12, 10, 0),
        product_category="companion_followup",
    )
    assert sent.get("called") is True  # inline 本机直发
    assert out["status"] == "sent"


def test_enqueue_onboarding_welcome_routes_to_assigned_node(fresh_db):
    from app.db import get_outbound_message
    from app.proactive import messaging

    _create_account("acc-welcome", node_id="aliyun2")
    out = messaging.enqueue_onboarding_welcome(
        account_id="acc-welcome",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-welcome",
        text="欢迎",
        now=datetime(2026, 6, 12, 10, 0),
    )
    assert out["status"] == "pending"
    assert out["node_id"] == "aliyun2"
    assert out["source"] == "onboarding_welcome"
    # UNIQUE idempotency_key 去重:重复入队不新建
    again = messaging.enqueue_onboarding_welcome(
        account_id="acc-welcome",
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key="session-acc-welcome",
        text="欢迎",
        now=datetime(2026, 6, 12, 10, 0),
    )
    assert again["id"] == out["id"]
    assert get_outbound_message(outbound_message_id=out["id"])["status"] == "pending"
