"""Tests for web_fetch tool (schema + SSRF + handler)."""
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------- Schema / registry ----------

def test_web_fetch_in_registry():
    from app.tools.registry import get_spec
    spec = get_spec("web_fetch")
    assert spec is not None
    assert spec.handler_module == "app.tools.web_fetch_handlers"


def test_web_fetch_schema_has_url_required():
    from app.tools.definitions import get_web_fetch_tools
    tools = get_web_fetch_tools()
    assert len(tools) == 1
    params = tools[0]["function"]["parameters"]
    assert "url" in params["required"]


def test_web_fetch_schema_optional_extract_mode():
    from app.tools.definitions import get_web_fetch_tools
    tools = get_web_fetch_tools()
    props = tools[0]["function"]["parameters"]["properties"]
    assert "extractMode" in props
    assert "maxChars" in props


def test_read_in_registry():
    from app.tools.registry import get_spec
    spec = get_spec("read")
    assert spec is not None
    assert spec.handler_module == "app.tools.read_handlers"


# ---------- web_fetch handler ----------

def _make_httpx_response(status_code, content_type, body_text):
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_redirect = False
    resp.headers = {"content-type": content_type}
    resp.read.return_value = body_text.encode("utf-8")
    return resp


def _handle_fetch(args, response_mock=None):
    from app.tools.web_fetch_handlers import handle_web_fetch
    settings_mock = MagicMock()
    settings_mock.web_fetch_timeout_seconds = 8.0
    settings_mock.web_fetch_connect_timeout_seconds = 3.0
    settings_mock.web_fetch_max_response_bytes = 524288
    settings_mock.web_fetch_max_chars = 6000
    settings_mock.web_fetch_max_redirects = 3
    settings_mock.llm_connect_timeout_seconds = 3.0

    if response_mock is None:
        response_mock = _make_httpx_response(200, "text/plain", "hello world")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = response_mock

    with patch("app.tools.web_fetch_handlers.settings", settings_mock):
        with patch("app.tools.web_fetch_handlers.assert_public_url"):  # bypass DNS
            with patch("app.tools.web_fetch_handlers.httpx.Client", return_value=client_mock):
                return handle_web_fetch(args, ctx=None)


def test_fetch_success_plain_text():
    result = _handle_fetch({"url": "https://example.com/"})
    assert result["status"] == 200
    assert result["externalContent"]["wrapped"] is True
    assert result["externalContent"]["source"] == "web_fetch"
    assert "hello world" in result["text"]
    assert "EXTERNAL_UNTRUSTED_CONTENT" in result["text"]


def test_fetch_missing_url_returns_error():
    from app.tools.web_fetch_handlers import handle_web_fetch
    result = handle_web_fetch({}, ctx=None)
    assert result["status"] == "failed"
    assert "url" in result["error"]


def test_fetch_ssrf_url_blocked():
    from app.tools.web_fetch_handlers import handle_web_fetch
    result = handle_web_fetch({"url": "http://127.0.0.1/"}, ctx=None)
    assert result["status"] == "failed"
    assert "安全策略" in result["error"]


def test_fetch_truncation():
    long_body = "x" * 10000
    resp = _make_httpx_response(200, "text/plain", long_body)
    # maxChars 下限被 clamp 到 200；传 500 验证实际截断生效
    result = _handle_fetch({"url": "https://example.com/", "maxChars": 500}, response_mock=resp)
    assert result["truncated"] is True
    assert result["length"] <= 500
    assert result["rawLength"] > 500


def test_fetch_html_strips_tags():
    html = "<html><body><p>Hello <b>World</b></p></body></html>"
    resp = _make_httpx_response(200, "text/html; charset=utf-8", html)
    result = _handle_fetch({"url": "https://example.com/"}, response_mock=resp)
    assert result["status"] == 200
    assert "Hello" in result["text"]
    # Tags should be stripped
    assert "<b>" not in result["text"]


def test_fetch_json_content_type():
    body = json.dumps({"temp": 28, "desc": "sunny"})
    resp = _make_httpx_response(200, "application/json", body)
    result = _handle_fetch({"url": "https://wttr.in/Beijing?format=j1"}, response_mock=resp)
    assert result["status"] == 200
    assert "28" in result["text"]


def test_fetch_result_already_wrapped_not_double_wrapped():
    """Batch A projection は externalContent.wrapped=true を検出して二重包裹しない。"""
    from app.tools.external_content import project_tool_result_for_llm
    resp = _make_httpx_response(200, "text/plain", "page content")
    result = _handle_fetch({"url": "https://example.com/"}, response_mock=resp)
    # project_tool_result_for_llm への入力が already-wrapped → 透过しかない
    projected = project_tool_result_for_llm("web_fetch", result)
    parsed = json.loads(projected)
    # externalContent が1つだけ（二重包裹なし）
    assert "externalContent" in parsed
    # ネストしていない
    assert not isinstance(parsed.get("externalContent", {}).get("externalContent"), dict)


def test_fetch_relative_redirect_resolved_against_current_url():
    from app.tools.web_fetch_handlers import handle_web_fetch

    settings_mock = MagicMock()
    settings_mock.web_fetch_timeout_seconds = 8.0
    settings_mock.web_fetch_connect_timeout_seconds = 2.5
    settings_mock.web_fetch_max_response_bytes = 524288
    settings_mock.web_fetch_max_chars = 6000
    settings_mock.web_fetch_max_redirects = 3

    redirect_resp = MagicMock()
    redirect_resp.is_redirect = True
    redirect_resp.headers = {"location": "/final"}

    final_resp = _make_httpx_response(200, "text/plain", "redirect ok")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [redirect_resp, final_resp]

    with patch("app.tools.web_fetch_handlers.settings", settings_mock):
        with patch("app.tools.web_fetch_handlers.assert_public_url") as guard_mock:
            with patch("app.tools.web_fetch_handlers.httpx.Client", return_value=client_mock) as client_cls:
                result = handle_web_fetch({"url": "https://example.com/start"}, ctx=None)

    assert result["status"] == 200
    assert result["finalUrl"] == "https://example.com/final"
    assert [call.args[0] for call in guard_mock.call_args_list] == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    timeout_arg = client_cls.call_args.kwargs["timeout"]
    assert timeout_arg.connect == 2.5


# ---------- prompt builder skills block ----------

def test_prompt_builder_skills_block_dict_format():
    from app.prompt_builder import PromptBuilder
    pb = PromptBuilder()
    out = pb.build(skills=[
        {"name": "weather", "description": "查天气", "location": "skills/weather/SKILL.md", "version": "abc123"}
    ])
    assert "【Skills】" in out
    assert "<available_skills>" in out
    assert "skills/weather/SKILL.md" in out
    assert "weather" in out
    assert "read" in out  # 提示用 read 工具


def test_prompt_builder_skills_block_skip_when_empty():
    from app.prompt_builder import PromptBuilder
    pb = PromptBuilder()
    out = pb.build(skills=[])
    assert "【Skills】" not in out
    assert "<available_skills>" not in out
