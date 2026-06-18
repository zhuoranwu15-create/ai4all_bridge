"""Tests for _make_tool_thinking_sender (turn_service helper)."""
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
        with patch("app.turn_service.should_inline_dispatch_for_account", return_value=True):
            result = _make_tool_thinking_sender(
                identity=identity,
                account_id="acc-1",
                openclaw_session_key="sk-1",
            )
    assert result is None


def test_returns_none_when_not_inline_dispatch():
    """非 inline dispatch 节点（中心节点/远程账号）返回 None。"""
    from app.turn_service import _make_tool_thinking_sender

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.should_inline_dispatch_for_account", return_value=False):
            result = _make_tool_thinking_sender(
                identity=identity,
                account_id="acc-remote",
                openclaw_session_key="sk-1",
            )
    assert result is None


def test_returns_callable_for_inline_account_with_user_id():
    """inline 节点且有 to_user_id 时返回可调用对象。"""
    from app.turn_service import _make_tool_thinking_sender

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.should_inline_dispatch_for_account", return_value=True):
            result = _make_tool_thinking_sender(
                identity=identity,
                account_id="acc-1",
                openclaw_session_key="sk-1",
            )
    assert callable(result)


def test_send_runs_in_background_thread_not_caller_thread():
    """callback 触发后 send_weixin_text 在后台线程执行，不阻塞调用方线程。"""
    from app.turn_service import _make_tool_thinking_sender

    caller_thread_id = threading.get_ident()
    send_thread_ids = []
    send_started = threading.Event()

    def fake_send(**kwargs):
        send_thread_ids.append(threading.get_ident())
        send_started.set()

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.should_inline_dispatch_for_account", return_value=True):
            with patch("app.turn_service.send_weixin_text", side_effect=fake_send):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-1",
                    openclaw_session_key="sk-1",
                )
                sender(["web_search"])

    send_started.wait(timeout=2)
    assert send_thread_ids, "send_weixin_text was never called"
    assert send_thread_ids[0] != caller_thread_id, "send_weixin_text ran in caller thread (blocking)"


def test_send_failure_does_not_propagate():
    """send_weixin_text 失败时 callback 不抛异常到调用方。"""
    from app.turn_service import _make_tool_thinking_sender

    done = threading.Event()

    def boom(**kwargs):
        done.set()
        raise RuntimeError("网关挂了")

    identity = _make_identity()
    with patch("app.turn_service.settings", _settings_mock()):
        with patch("app.turn_service.should_inline_dispatch_for_account", return_value=True):
            with patch("app.turn_service.send_weixin_text", side_effect=boom):
                sender = _make_tool_thinking_sender(
                    identity=identity,
                    account_id="acc-1",
                    openclaw_session_key="sk-1",
                )
                # 不应抛异常
                sender(["web_search"])

    done.wait(timeout=2)
