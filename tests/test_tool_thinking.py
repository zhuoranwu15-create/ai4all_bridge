"""Tests for _make_tool_thinking_sender (turn_service helper).

暂态消息已统一经 node_gateway.node_send_text 按账号归属节点发送(本机直调/远程 push)，
不再因「非本机账号」静默跳过。仅在 to_user_id 为空时返回 None。
"""
import threading
from unittest.mock import MagicMock, patch


def _make_identity(*, chat_id="chat-1", sender_id="sender-1", channel_account_id="bot-1", channel="openclaw-weixin"):
    identity = MagicMock()
    identity.chat_id = chat_id
    identity.sender_id = sender_id
    identity.channel_account_id = channel_account_id
    identity.channel = channel
    return identity


def _settings_mock(*, gateway_timeout_ms=5000):
    s = MagicMock()
    s.openclaw_gateway_call_timeout_ms = gateway_timeout_ms
    return s


def test_returns_none_when_no_to_user_id():
    """chat_id 和 sender_id 均为空时返回 None，不尝试发送。"""
    from app.turn_service import _make_tool_thinking_sender

    identity = _make_identity(chat_id="", sender_id="")
    with patch("app.turn_service.settings", _settings_mock()):
        result = _make_tool_thinking_sender(
            identity=identity,
            account_id="acc-1",
            openclaw_session_key="sk-1",
        )
    assert result is None


def test_returns_callable_for_remote_account():
    """远程账号(归属其它节点)不再返回 None——统一经 node_send_text push，行为与本机一致。"""
    from app.turn_service import _make_tool_thinking_sender

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        result = _make_tool_thinking_sender(
            identity=identity,
            account_id="acc-remote",
            openclaw_session_key="sk-1",
        )
    assert callable(result)


def test_returns_callable_with_user_id():
    """有 to_user_id 时返回可调用对象。"""
    from app.turn_service import _make_tool_thinking_sender

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        result = _make_tool_thinking_sender(
            identity=identity,
            account_id="acc-1",
            openclaw_session_key="sk-1",
        )
    assert callable(result)


def test_send_routes_to_account_node():
    """触发时经 node_send_text 发，node_id 取自账号归属节点(远程账号 → 远程节点)。"""
    from app.turn_service import _make_tool_thinking_sender

    captured = {}
    done = threading.Event()

    def fake_send(**kwargs):
        captured.update(kwargs)
        done.set()

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.resolve_node_for_account", return_value="aliyun2"):
            with patch("app.turn_service.node_gateway.node_send_text", side_effect=fake_send):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-remote",
                    openclaw_session_key="sk-1",
                )
                sender(["web_search"])

    assert done.wait(timeout=2), "node_send_text was never called"
    assert captured.get("node_id") == "aliyun2"
    assert captured.get("to_user_id") == "chat-1"
    assert captured.get("account_id") == "bot-1"
    assert captured.get("channel") == "openclaw-weixin"
    assert "稍等" in captured.get("text", "") or "找找" in captured.get("text", "") or "搜索" in captured.get("text", "")


def test_unassigned_account_falls_back_to_default_node():
    """未分配 assigned_node_id 的账号:归属解析回落 settings.default_node_id(与 enqueue 一致)。"""
    from app.turn_service import _make_tool_thinking_sender

    captured = {}
    done = threading.Event()

    def fake_send(**kwargs):
        captured.update(kwargs)
        done.set()

    s = _settings_mock()
    s.default_node_id = "aliyun2"
    identity = _make_identity()
    with patch("app.turn_service.settings", s):
        with patch("app.turn_service.resolve_node_for_account", return_value=None):
            with patch("app.turn_service.node_gateway.node_send_text", side_effect=fake_send):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-unassigned",
                    openclaw_session_key="sk-1",
                )
                sender(["web_search"])

    assert done.wait(timeout=2)
    assert captured.get("node_id") == "aliyun2"


def test_send_runs_in_background_thread_not_caller_thread():
    """callback 触发后 node_send_text 在后台线程执行，不阻塞调用方线程。"""
    from app.turn_service import _make_tool_thinking_sender

    caller_thread_id = threading.get_ident()
    send_thread_ids = []
    send_started = threading.Event()

    def fake_send(**kwargs):
        send_thread_ids.append(threading.get_ident())
        send_started.set()

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.resolve_node_for_account", return_value=None):
            with patch("app.turn_service.node_gateway.node_send_text", side_effect=fake_send):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-1",
                    openclaw_session_key="sk-1",
                )
                sender(["web_search"])

    send_started.wait(timeout=2)
    assert send_thread_ids, "node_send_text was never called"
    assert send_thread_ids[0] != caller_thread_id, "node_send_text ran in caller thread (blocking)"


def test_send_failure_does_not_propagate():
    """node_send_text 失败时 callback 不抛异常到调用方(远程不可达等可丢)。"""
    from app.turn_service import _make_tool_thinking_sender

    done = threading.Event()

    def boom(**kwargs):
        done.set()
        raise RuntimeError("网关挂了")

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.resolve_node_for_account", return_value=None):
            with patch("app.turn_service.node_gateway.node_send_text", side_effect=boom):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-1",
                    openclaw_session_key="sk-1",
                )
                # 不应抛异常
                sender(["web_search"])

    done.wait(timeout=2)
