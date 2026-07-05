"""入站收口回归：未绑定/已解绑的微信入站不应创建账号或继续回复。

覆盖 resolve_account_id_for_inbound_channel_identity 返回 None 后，
handle_openclaw_turn 在严格模式（openclaw_inbound_require_binding=True）下的收口行为。
"""

from unittest.mock import patch

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _unbound_payload(session_key: str, text: str = "你好") -> dict:
    # 模拟已解绑/未绑定：channel_account_id 存在但无任何 completed binding intent。
    return {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot-unbound",
        "account_id": "bot-unbound",
        "session_key": session_key,
        "sender_id": f"sender-{session_key}",
        "chat_id": f"peer-{session_key}",
        "chat_type": "private",
        "message_type": "text",
        "message_id": "unbound-msg-1",
        "text": text,
        "raw": {"event_type": "message", "content": text},
    }


def test_unbound_inbound_no_reply_and_no_account_created(client, fresh_db):
    """严格模式：无绑定入站直接 ignored/no_reply，且不创建账号、不落盘 profile。"""
    from app.db import get_account
    from app.user_profiles import account_profile_dir

    fresh_db.openclaw_inbound_require_binding = True
    session_key = "agent:main:openclaw-weixin:bot-unbound:direct:peer-x@im.wechat"

    with patch("app.turn_service.node_gateway.node_send_text") as mock_send, \
         patch("app.turn_service.generate_reply", return_value="不该被调用") as mock_reply:
        res = client.post(
            "/openclaw/turn",
            json=_unbound_payload(session_key),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ignored"
    assert data["no_reply"] is True
    assert data["metadata"]["reason"] == "no_binding"

    # 收口必须发生在账号创建/回复之前。
    assert get_account(account_id=session_key) is None
    assert not account_profile_dir(session_key).exists()
    mock_reply.assert_not_called()
    mock_send.assert_not_called()


def test_unbound_inbound_falls_back_when_guard_disabled(client, fresh_db):
    """开关关闭（本地调试/现有测试）：保留 session_key 兜底，账号照常创建。"""
    from app.db import get_account

    fresh_db.openclaw_inbound_require_binding = False
    session_key = "agent:main:openclaw-weixin:bot-unbound:direct:peer-y@im.wechat"

    with patch("app.turn_service.node_gateway.node_send_text", return_value={"messageId": "m1"}):
        res = client.post(
            "/openclaw/turn",
            json=_unbound_payload(session_key),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    assert res.json()["status"] != "ignored"
    # 兜底创建的账号 id == session_key。
    assert get_account(account_id=session_key) is not None


def test_unbound_inbound_reuses_existing_account_for_same_session_key(client, fresh_db):
    """开关关闭时，若该 session_key 此前已落过账号（如 debug 建号未走
    binding），兜底应复用该账号，而不是把 session_key 当新账号建一个
    影子账号（此前 bug：debug 账号的 onboarding 状态永远停在 pending，
    真实对话全落在影子账号上）。"""
    from app.db import get_account, get_or_create_session

    fresh_db.openclaw_inbound_require_binding = False
    account_id = "debug-preexisting"
    session_key = f"openclaw-weixin:{account_id}:debug-sender-{account_id}"

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id=f"debug-sender-{account_id}",
        sender_name=None,
        chat_id=f"debug-sender-{account_id}",
        session_key=session_key,
    )

    with patch("app.turn_service.node_gateway.node_send_text", return_value={"messageId": "m1"}):
        res = client.post(
            "/openclaw/turn",
            json=_unbound_payload(session_key),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    assert res.json()["status"] != "ignored"
    # 复用预先存在的账号，而不是新建一个以 session_key 为 id 的影子账号。
    assert get_account(account_id=account_id) is not None
    assert get_account(account_id=session_key) is None
