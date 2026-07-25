"""多机接入 Phase 1:加性 schema 迁移 + 按节点出站认领 + node_id 解析。

覆盖:access_nodes/列迁移幂等、claim_pending_outbound_by_node 抢占语义、
enqueue_proactive_text 的 node_id 解析(显式 > 账号归属 > default_node_id 兜底 > None)。
"""
from datetime import datetime
from unittest.mock import patch

import pytest


from tests.factories import create_account as _create_account


def _enqueue_row(account_id, node_id, idem, *, status="pending", scheduled_at=None):
    from app.db import create_outbound_message

    return create_outbound_message(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{account_id}",
        source="reminder",
        text="t",
        idempotency_key=idem,
        quota_date="2026-06-12",
        status=status,
        scheduled_at=scheduled_at,
        node_id=node_id,
    )


# ===== 迁移 =====

def test_migration_adds_node_columns_table_index_idempotent(fresh_db):
    from app.db import connect, init_db

    init_db()  # 二次调用应幂等(加列/建表不报错)
    with connect() as conn:
        out_cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbound_messages)")}
        acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        idxs = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"node_id", "claimed_at"} <= out_cols
    assert "assigned_node_id" in acc_cols
    assert "access_nodes" in tables
    assert "ix_outbound_messages_node_dispatch" in idxs


# ===== claim_pending_outbound_by_node =====

def test_claim_is_node_scoped_and_no_double_claim(fresh_db):
    from app.db import claim_pending_outbound_by_node

    _create_account("acc-a")
    _create_account("acc-b")
    _enqueue_row("acc-a", "aliyun1", "a1")
    _enqueue_row("acc-a", "aliyun1", "a2")
    _enqueue_row("acc-b", "aliyun2", "b1")

    c1 = claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60)
    assert {r["idempotency_key"] for r in c1} == {"a1", "a2"}
    assert all(r["status"] == "sending" and r["attempts"] == 1 for r in c1)

    # 同 node 第二次:行已 sending 且未 stale → 不重复领
    assert claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60) == []

    # 另一节点只领自己 node_id 的行(天然互斥)
    c2 = claim_pending_outbound_by_node(node_id="aliyun2", batch_size=10, claim_timeout_seconds=60)
    assert {r["idempotency_key"] for r in c2} == {"b1"}


def test_claim_recovers_stale_sending(fresh_db):
    from app.db import claim_pending_outbound_by_node, connect

    _create_account("acc-s")
    row = _enqueue_row("acc-s", "aliyun1", "s1")
    first = claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60)
    assert len(first) == 1 and first[0]["attempts"] == 1
    # 未超时不可重领
    assert claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60) == []
    # claimed_at 推到很久以前 → 超时可被回收重领,attempts 累加
    with connect() as conn:
        conn.execute(
            "UPDATE outbound_messages SET claimed_at = '2000-01-01 00:00:00' WHERE id = ?",
            (row["id"],),
        )
    again = claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60)
    assert len(again) == 1 and again[0]["attempts"] == 2


def test_claim_respects_max_attempts(fresh_db):
    from app.db import claim_pending_outbound_by_node, connect

    _create_account("acc-m")
    row = _enqueue_row("acc-m", "aliyun1", "m1")
    with connect() as conn:
        conn.execute("UPDATE outbound_messages SET attempts = 5 WHERE id = ?", (row["id"],))
    assert (
        claim_pending_outbound_by_node(
            node_id="aliyun1", batch_size=10, claim_timeout_seconds=60, max_attempts=5
        )
        == []
    )


def test_claim_honors_scheduled_at(fresh_db):
    from app.db import claim_pending_outbound_by_node

    _create_account("acc-f")
    _enqueue_row("acc-f", "aliyun1", "future", scheduled_at="2999-01-01 00:00:00")
    _enqueue_row("acc-f", "aliyun1", "past", scheduled_at="2000-01-01 00:00:00")
    claimed = claim_pending_outbound_by_node(node_id="aliyun1", batch_size=10, claim_timeout_seconds=60)
    assert {r["idempotency_key"] for r in claimed} == {"past"}  # 未到点的 future 不领


def test_claim_empty_for_unknown_or_blank_node(fresh_db):
    from app.db import claim_pending_outbound_by_node

    assert claim_pending_outbound_by_node(node_id="ghost", batch_size=10, claim_timeout_seconds=60) == []
    assert claim_pending_outbound_by_node(node_id="", batch_size=10, claim_timeout_seconds=60) == []


# ===== node 登记 helper =====

def test_upsert_access_node_heartbeat_patches_only_provided(fresh_db):
    from app.db import pick_node, upsert_access_node

    upsert_access_node(node_id="aliyun2", base_url="http://aliyun2:8190", session_count=3, max_sessions=100)
    upsert_access_node(node_id="aliyun1", base_url="local", session_count=1, max_sessions=100)
    # 心跳只带 session_count,不应抹掉 base_url
    node = upsert_access_node(node_id="aliyun2", session_count=5)
    assert node["base_url"] == "http://aliyun2:8190"
    assert node["session_count"] == 5
    # pick:session_count 最小者
    assert pick_node() == "aliyun1"
    # preferred 命中 online
    assert pick_node(preferred_node_id="aliyun2") == "aliyun2"
    # preferred 不存在 → 回落最闲
    assert pick_node(preferred_node_id="ghost") == "aliyun1"


# ===== enqueue_proactive_text 的 node_id 解析 =====

def _enqueue(account_id, idem, node_id=None):
    from app.products.zhaoxi.proactive.delivery.outbound import enqueue_proactive_text

    return enqueue_proactive_text(
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@im.wechat",
        session_key=f"session-{account_id}",
        source="account_check",
        text="hi",
        idempotency_key=idem,
        now=datetime(2026, 6, 12, 10, 0),
        product_category="companion_followup",
        node_id=node_id,
    )


def test_enqueue_fills_node_id_from_account_assignment(fresh_db):
    with patch("app.db.settings", fresh_db), patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db):
        _create_account("acc-assigned", node_id="aliyun2")
        out = _enqueue("acc-assigned", "n-assigned")
    assert out["node_id"] == "aliyun2"


def test_enqueue_falls_back_to_default_node_id(fresh_db):
    fresh_db.default_node_id = "aliyun1"
    with patch("app.db.settings", fresh_db), patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db):
        _create_account("acc-default")  # 无归属
        out = _enqueue("acc-default", "n-default")
    assert out["node_id"] == "aliyun1"


def test_enqueue_explicit_node_id_wins(fresh_db):
    fresh_db.default_node_id = "aliyun1"
    with patch("app.db.settings", fresh_db), patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db):
        _create_account("acc-explicit", node_id="aliyun2")
        out = _enqueue("acc-explicit", "n-explicit", node_id="aliyunX")
    assert out["node_id"] == "aliyunX"  # 显式 > 账号归属 > 兜底


def test_enqueue_node_id_none_in_standalone(fresh_db):
    # conftest 默认 default_node_id="" → standalone 不写 node_id,行为不变
    with patch("app.db.settings", fresh_db), patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db):
        _create_account("acc-standalone")
        out = _enqueue("acc-standalone", "n-standalone")
    assert out["node_id"] is None


# ===== Phase 2:node_gateway 派发层 =====

def test_node_gateway_standalone_always_local(monkeypatch):
    """standalone 下即便 node_id 是远程名,也走本机直调(零网络跳,保证零回归)。"""
    from app.platform.gateways import node_gateway

    calls = {}

    def _fake_start(**kw):
        calls["start"] = kw
        return {"qrDataUrl": "x"}

    monkeypatch.setattr(node_gateway.openclaw_gateway, "start_weixin_qr_login", _fake_start)
    # 显式注入 standalone：不能依赖进程 ambient settings——开发机 .env 可能设了 central,node，
    # 会让 node_id="aliyun2" 被判为远程节点并真发 HTTP。与下方兄弟测试同款注入方式。
    from app.config import Settings
    monkeypatch.setattr(node_gateway, "settings", Settings(ai4all_role="standalone"))
    out = node_gateway.node_start_qr(
        node_id="aliyun2", account_id="sess-1", gateway_timeout_ms=5000, start_timeout_ms=5000
    )
    assert out["qrDataUrl"] == "x"
    assert calls["start"]["account_id"] == "sess-1"  # 本机直调


def test_node_gateway_local_when_node_id_matches(monkeypatch):
    from app.platform.gateways import node_gateway
    from app.config import Settings

    monkeypatch.setattr(node_gateway, "settings", Settings(ai4all_role="central,node", node_id="aliyun1"))
    called = {}
    monkeypatch.setattr(
        node_gateway.openclaw_gateway,
        "logout_weixin_account",
        lambda **kw: called.update({"logout": kw}) or {"ok": True},
    )
    out = node_gateway.node_logout(
        node_id="aliyun1", account_id="a", channel="openclaw-weixin", timeout_ms=5000
    )
    assert out["ok"] is True and called["logout"]["account_id"] == "a"


def test_node_gateway_remote_unregistered_raises(monkeypatch, fresh_db):
    """非本机 node_id 且 access_nodes 无 base_url 登记 → 抛 OpenClawGatewayError
    (映射进 main.py 既有 except → set_binding_intent_error,行为一致)。"""
    from app.platform.gateways import node_gateway
    from app.config import Settings
    from app.platform.gateways.openclaw import OpenClawGatewayError

    monkeypatch.setattr(node_gateway, "settings", Settings(ai4all_role="central", node_id="aliyun1"))
    with patch("app.db.settings", fresh_db):
        with pytest.raises(OpenClawGatewayError):
            node_gateway.node_start_qr(
                node_id="aliyun2", account_id="x", gateway_timeout_ms=5000, start_timeout_ms=5000
            )


# ===== Phase 2:节点面向端点(鉴权 + 状态机)=====

_BEARER = {"Authorization": "Bearer test-secret"}


def _seed_outbound(node_id, idem, account_id="acc-node"):
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


def test_node_endpoints_require_bearer(client):
    assert client.post("/node/outbound/claim", json={"node_id": "aliyun2", "batch": 10}).status_code == 401
    assert client.post("/node/heartbeat", json={"node_id": "aliyun2"}).status_code == 401
    ok = client.post("/node/outbound/claim", json={"node_id": "aliyun2", "batch": 10}, headers=_BEARER)
    assert ok.status_code == 200


def test_node_claim_result_roundtrip(client):
    _seed_outbound("aliyun2", "om-1")
    claimed = client.post(
        "/node/outbound/claim", json={"node_id": "aliyun2", "batch": 10}, headers=_BEARER
    ).json()["messages"]
    assert [m["idempotency_key"] for m in claimed] == ["om-1"]
    assert claimed[0]["status"] == "sending"
    mid = claimed[0]["id"]

    res = client.post(
        f"/node/outbound/{mid}/result",
        json={"status": "sent", "gateway_message_id": "gw-9"},
        headers=_BEARER,
    ).json()
    assert res["outbound_message"]["status"] == "sent"
    assert res["outbound_message"]["gateway_message_id"] == "gw-9"

    # 已 sent → 再 claim 不返回
    again = client.post(
        "/node/outbound/claim", json={"node_id": "aliyun2", "batch": 10}, headers=_BEARER
    ).json()["messages"]
    assert again == []


def test_node_result_failed_marks_failed(client):
    seeded = _seed_outbound("aliyun2", "om-f")
    client.post("/node/outbound/claim", json={"node_id": "aliyun2", "batch": 10}, headers=_BEARER)
    res = client.post(
        f"/node/outbound/{seeded['id']}/result",
        json={"status": "failed", "error": "rate_limited: ret=-2"},
        headers=_BEARER,
    ).json()
    assert res["outbound_message"]["status"] == "failed"
    assert "rate_limited" in (res["outbound_message"]["error"] or "")


def test_node_heartbeat_upserts_access_node(client):
    node = client.post(
        "/node/heartbeat",
        json={
            "node_id": "aliyun2",
            "session_count": 7,
            "base_url": "http://aliyun2:8190",
            "egress_ip": "1.2.3.4",
            "max_sessions": 100,
        },
        headers=_BEARER,
    ).json()["node"]
    assert node["session_count"] == 7
    assert node["base_url"] == "http://aliyun2:8190"
    # 二次心跳只带 session_count 不抹 base_url
    node2 = client.post(
        "/node/heartbeat", json={"node_id": "aliyun2", "session_count": 9}, headers=_BEARER
    ).json()["node"]
    assert node2["session_count"] == 9 and node2["base_url"] == "http://aliyun2:8190"


# ===== should_inline_dispatch_for_account（per-account inline 判定；修复全局 inline bug）=====
# bug：is_inline_dispatch 是全局开关，central+node 开 LOCAL_NODE_INLINE_DISPATCH 时，
# 发给远程节点账号的主动消息会被本机 inline 误发（本机无会话→失败 + 抢走远程 pull 队列行）。


def _set_role(fresh_db, *, role, node_id="", default_node_id="", inline=False):
    fresh_db.ai4all_role = role
    fresh_db.node_id = node_id
    fresh_db.default_node_id = default_node_id
    fresh_db.local_node_inline_dispatch = inline


def test_inline_standalone_always_true(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="standalone")
    # 单机：任何账号（含未知 / None）都 inline，行为逐字节不变
    assert should_inline_dispatch_for_account("anything", fresh_db) is True
    assert should_inline_dispatch_for_account(None, fresh_db) is True


def test_inline_central_node_local_account_true(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="central,node", node_id="aliyun1", default_node_id="aliyun1", inline=True)
    _create_account("acc-local", node_id="aliyun1")
    assert should_inline_dispatch_for_account("acc-local", fresh_db) is True


def test_inline_central_node_remote_account_false(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="central,node", node_id="aliyun1", default_node_id="aliyun1", inline=True)
    _create_account("acc-remote", node_id="aliyun2")
    # 关键回归：归属远程节点的账号必须 False（走 enqueue，不在本机误发）
    assert should_inline_dispatch_for_account("acc-remote", fresh_db) is False


def test_inline_unassigned_uses_default_node(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="central,node", node_id="aliyun1", default_node_id="aliyun1", inline=True)
    _create_account("acc-unassigned")  # 无 assigned_node_id → 回落 default
    assert should_inline_dispatch_for_account("acc-unassigned", fresh_db) is True  # default=本机
    fresh_db.default_node_id = "aliyun2"
    assert should_inline_dispatch_for_account("acc-unassigned", fresh_db) is False  # default=远程


def test_inline_disabled_when_flag_off(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="central,node", node_id="aliyun1", default_node_id="aliyun1", inline=False)
    _create_account("acc-local2", node_id="aliyun1")
    # 未开 LOCAL_NODE_INLINE_DISPATCH → 即便本机账号也 enqueue
    assert should_inline_dispatch_for_account("acc-local2", fresh_db) is False


def test_inline_central_only_false(fresh_db):
    from app.db import should_inline_dispatch_for_account

    _set_role(fresh_db, role="central", node_id="", default_node_id="aliyun1", inline=False)
    _create_account("acc-c", node_id="aliyun1")
    assert should_inline_dispatch_for_account("acc-c", fresh_db) is False


def test_dispatch_proactive_remote_account_enqueues_not_sends(fresh_db):
    """dispatch_proactive_text 对远程账号必须走 enqueue（pending + node_id=远程），
    而不是 send_proactive_text（会在本机误发）。这是本次 bug 的端到端回归守卫。"""
    from app.products.zhaoxi.proactive.delivery.outbound import dispatch_proactive_text

    _set_role(fresh_db, role="central,node", node_id="aliyun1", default_node_id="aliyun1", inline=True)
    with patch("app.db.settings", fresh_db), patch("app.products.zhaoxi.proactive.delivery.outbound.settings", fresh_db):
        _create_account("acc-remote-dispatch", node_id="aliyun2")
        # 若误走 inline，send_proactive_text 会调用真实 send_weixin_text（openclaw）→ 必然炸；
        # 走 enqueue 则只建 pending 行，不碰 openclaw。
        out = dispatch_proactive_text(
            account_id="acc-remote-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-remote",
            to_user_id="user@im.wechat",
            session_key="session-acc-remote-dispatch",
            source="reminder",
            text="远程账号主动消息",
            idempotency_key="disp-remote-1",
            now=datetime(2026, 6, 12, 10, 0),
            bypass_quiet_hours=True,
            product_category="user_reminder",
        )
    assert out["status"] == "pending"
    assert out["node_id"] == "aliyun2"
