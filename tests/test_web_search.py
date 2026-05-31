from unittest.mock import MagicMock, patch

import httpx

from app.turn_context import TurnContext


def _setup_ctx(account_id: str):
    from app.db import get_or_create_session

    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = f"sk-{account_id}"
    session = get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat-1",
        session_key=f"sk-{account_id}",
    )["session"]
    return TurnContext(
        account_id=account_id,
        account={"id": account_id},
        session=session,
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="search",
        today="2026-05-31",
        business_day="2026-05-31",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


def test_parse_duckduckgo_html_extracts_results():
    from app.web_search import parse_duckduckgo_html

    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>
      <a class="result__snippet">Snippet A</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.org/b">Example B</a>
      <a class="result__snippet">Snippet B</a>
    </div>
    """

    results = parse_duckduckgo_html(html, count=2)

    assert len(results) == 2
    assert results[0]["title"] == "Example A"
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["snippet"] == "Snippet A"
    assert results[1]["site_name"] == "example.org"


def test_parse_bing_rss_extracts_results():
    from app.web_search import parse_bing_rss

    xml = """<?xml version="1.0" encoding="utf-8" ?>
    <rss version="2.0">
      <channel>
        <item>
          <title>欧冠-巴黎圣日耳曼点球大战击败阿森纳成功卫冕</title>
          <link>https://sports.cctv.com/example</link>
          <description>巴黎圣日耳曼点球大战4-3，总比分5-4获胜。</description>
        </item>
        <item>
          <title>Duplicate</title>
          <link>https://sports.cctv.com/example</link>
          <description>duplicate</description>
        </item>
      </channel>
    </rss>"""

    results = parse_bing_rss(xml, count=5)

    assert len(results) == 1
    assert results[0]["title"] == "欧冠-巴黎圣日耳曼点球大战击败阿森纳成功卫冕"
    assert results[0]["url"] == "https://sports.cctv.com/example"
    assert results[0]["snippet"] == "巴黎圣日耳曼点球大战4-3，总比分5-4获胜。"
    assert results[0]["site_name"] == "sports.cctv.com"


def test_parse_baidu_ai_search_response_extracts_references():
    from app.web_search import parse_baidu_ai_search_response

    payload = {
        "references": [
            {
                "title": "欧冠-巴黎圣日耳曼点球大战击败阿森纳成功卫冕",
                "url": "https://sports.cctv.com/example",
                "content": "90分钟内战成1-1，点球大战4-3，总比分5-4。",
                "website": "央视网",
                "rerank_score": 0.88,
                "date": "2026-05-31 01:01:00",
            }
        ]
    }

    results = parse_baidu_ai_search_response(payload, count=3)

    assert len(results) == 1
    assert results[0]["title"].startswith("欧冠-巴黎圣日耳曼")
    assert results[0]["url"] == "https://sports.cctv.com/example"
    assert results[0]["snippet"] == "90分钟内战成1-1，点球大战4-3，总比分5-4。"
    assert results[0]["site_name"] == "央视网"
    assert results[0]["score"] == 0.88


def test_parse_aliyun_web_search_response_extracts_references_and_answer():
    from app.web_search import _extract_aliyun_answer, parse_aliyun_web_search_response

    payload = {
        "choices": [
            {
                "message": {
                    "content": "巴黎圣日耳曼点球大战总比分5-4击败阿森纳。"
                }
            }
        ],
        "search_info": {
            "search_results": [
                {
                    "title": "点球大战制胜！欧冠：大巴黎总分5-4阿森纳",
                    "url": "https://news.qq.com/example",
                    "snippet": "总比分1-1，点球大战4-3。",
                    "site_name": "腾讯新闻",
                }
            ]
        },
    }

    assert _extract_aliyun_answer(payload) == "巴黎圣日耳曼点球大战总比分5-4击败阿森纳。"
    results = parse_aliyun_web_search_response(payload, count=3)
    assert len(results) == 1
    assert results[0]["title"].startswith("点球大战制胜")
    assert results[0]["site_name"] == "腾讯新闻"


def test_parse_aliyun_web_search_response_extracts_iqs_page_items():
    from app.web_search import parse_aliyun_web_search_response

    payload = {
        "pageItems": [
            {
                "title": "杭州猫耳朵",
                "link": "https://m.maigoo.com/citiao/187671.html",
                "snippet": "杭州特色名小吃。",
                "hostname": "品牌MAIGOO",
                "publishedTime": "2026-05-28T08:23:51+08:00",
                "rerankScore": 0.99,
            }
        ],
        "searchInformation": {"searchTime": 248},
    }

    results = parse_aliyun_web_search_response(payload, count=3)

    assert len(results) == 1
    assert results[0]["title"] == "杭州猫耳朵"
    assert results[0]["url"] == "https://m.maigoo.com/citiao/187671.html"
    assert results[0]["snippet"] == "杭州特色名小吃。"
    assert results[0]["site_name"] == "品牌MAIGOO"
    assert results[0]["score"] == 0.99
    assert results[0]["published_at"] == "2026-05-28T08:23:51+08:00"


def test_http_status_error_message_extracts_provider_json_error():
    from app.web_search import _http_status_error_message

    request = httpx.Request("POST", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
    response = httpx.Response(
        401,
        request=request,
        json={
            "error": {
                "message": "Incorrect API key provided",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )

    message = _http_status_error_message("aliyun", response)

    assert "aliyun error invalid_api_key" in message
    assert "Incorrect API key provided" in message
    assert "HTTP 401" in message


def test_payload_error_extracts_baidu_style_error():
    from app.web_search import _payload_error

    assert _payload_error({"code": "PermissionDenied", "message": "invalid appbuilder token"}) == (
        "PermissionDenied: invalid appbuilder token"
    )


def test_web_search_handler_returns_structured_results_and_provider_trace(fresh_db):
    from app.db import create_tool_invocation, list_search_provider_runs
    from app.tools.web_search_handlers import handle_web_search

    ctx = _setup_ctx("ws-handler")
    invocation = create_tool_invocation(
        account_id=ctx.account_id,
        session_id=int(ctx.session["id"]),
        message_id=ctx.message_id,
        tool_call_id="call-web",
        tool_name="web_search",
        args={"query": "OpenClaw"},
    )
    provider_response = {
        "provider": "duckduckgo",
        "query": "OpenClaw",
        "results": [
            {
                "title": "OpenClaw",
                "url": "https://example.com/openclaw",
                "snippet": "OpenClaw result",
                "site_name": "example.com",
                "retrieved_at": "2026-05-31T00:00:00+00:00",
                "score": None,
            }
        ],
        "citations": [{"title": "OpenClaw", "url": "https://example.com/openclaw"}],
        "retrieved_at": "2026-05-31T00:00:00+00:00",
        "latency_ms": 42,
        "warnings": [],
    }

    with patch("app.tools.web_search_handlers.settings") as mock_settings, patch(
        "app.tools.web_search_handlers.duckduckgo_search",
        return_value=provider_response,
    ):
        mock_settings.web_search_default_provider = "duckduckgo"
        mock_settings.web_search_provider_order = ""
        mock_settings.web_search_provider_failover = True
        mock_settings.web_search_sync_timeout_seconds = 8.0
        mock_settings.web_search_max_results = 5
        result = handle_web_search(
            {"query": "OpenClaw", "count": 1},
            ctx,
            tool_call_id="call-web",
            tool_invocation_id=int(invocation["id"]),
        )

    assert result["status"] == "succeeded"
    assert result["provider"] == "duckduckgo"
    assert result["results"][0]["title"] == "OpenClaw"
    runs = list_search_provider_runs(
        account_id=ctx.account_id,
        tool_invocation_id=int(invocation["id"]),
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["request"]["query"] == "OpenClaw"


def test_web_search_handler_supports_bing_provider(fresh_db):
    from app.db import list_search_provider_runs
    from app.tools.web_search_handlers import handle_web_search

    ctx = _setup_ctx("ws-bing")
    provider_response = {
        "provider": "bing",
        "query": "欧冠决赛比分",
        "results": [
            {
                "title": "欧冠决赛比分",
                "url": "https://example.com/ucl",
                "snippet": "巴黎总比分5-4阿森纳。",
                "site_name": "example.com",
                "retrieved_at": "2026-05-31T00:00:00+00:00",
                "score": None,
            }
        ],
        "citations": [{"title": "欧冠决赛比分", "url": "https://example.com/ucl"}],
        "retrieved_at": "2026-05-31T00:00:00+00:00",
        "latency_ms": 32,
        "warnings": [],
    }

    with patch("app.tools.web_search_handlers.settings") as mock_settings, patch(
        "app.tools.web_search_handlers.bing_search",
        return_value=provider_response,
    ):
        mock_settings.web_search_default_provider = "bing"
        mock_settings.web_search_provider_order = ""
        mock_settings.web_search_provider_failover = True
        mock_settings.web_search_sync_timeout_seconds = 8.0
        mock_settings.web_search_max_results = 5
        result = handle_web_search({"query": "欧冠决赛比分", "count": 1}, ctx)

    assert result["status"] == "succeeded"
    assert result["provider"] == "bing"
    assert result["results"][0]["snippet"] == "巴黎总比分5-4阿森纳。"
    runs = list_search_provider_runs(account_id=ctx.account_id)
    assert len(runs) == 1
    assert runs[0]["provider"] == "bing"
    assert runs[0]["status"] == "succeeded"


def test_web_search_handler_records_provider_failure(fresh_db):
    from app.db import list_search_provider_runs
    from app.tools.web_search_handlers import handle_web_search
    from app.web_search import DuckDuckGoSearchError

    ctx = _setup_ctx("ws-failed")

    with patch("app.tools.web_search_handlers.settings") as mock_settings, patch(
        "app.tools.web_search_handlers.duckduckgo_search",
        side_effect=DuckDuckGoSearchError("no results"),
    ):
        mock_settings.web_search_default_provider = "duckduckgo"
        mock_settings.web_search_provider_order = ""
        mock_settings.web_search_provider_failover = True
        mock_settings.web_search_sync_timeout_seconds = 8.0
        mock_settings.web_search_max_results = 5
        result = handle_web_search({"query": "missing"}, ctx)

    assert result["status"] == "failed"
    assert result["error"] == "no results"
    runs = list_search_provider_runs(account_id=ctx.account_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] == "no results"


def test_web_search_handler_fails_over_to_bing(fresh_db):
    from app.db import list_search_provider_runs
    from app.tools.web_search_handlers import handle_web_search
    from app.web_search import DuckDuckGoSearchError

    ctx = _setup_ctx("ws-failover")
    provider_response = {
        "provider": "bing",
        "query": "OpenClaw",
        "results": [
            {
                "title": "OpenClaw fallback",
                "url": "https://example.com/fallback",
                "snippet": "fallback result",
                "site_name": "example.com",
                "retrieved_at": "2026-05-31T00:00:00+00:00",
                "score": None,
            }
        ],
        "citations": [{"title": "OpenClaw fallback", "url": "https://example.com/fallback"}],
        "retrieved_at": "2026-05-31T00:00:00+00:00",
        "latency_ms": 28,
        "warnings": [],
    }

    with patch("app.tools.web_search_handlers.settings") as mock_settings, patch(
        "app.tools.web_search_handlers.duckduckgo_search",
        side_effect=DuckDuckGoSearchError("duck failed"),
    ), patch(
        "app.tools.web_search_handlers.bing_search",
        return_value=provider_response,
    ):
        mock_settings.web_search_default_provider = "duckduckgo"
        mock_settings.web_search_provider_order = "duckduckgo,bing"
        mock_settings.web_search_provider_failover = True
        mock_settings.web_search_sync_timeout_seconds = 8.0
        mock_settings.web_search_max_results = 5
        result = handle_web_search({"query": "OpenClaw", "count": 1}, ctx)

    assert result["status"] == "succeeded"
    assert result["provider"] == "bing"
    assert result["attempts"] == [{"provider": "duckduckgo", "status": "failed", "error": "duck failed"}]
    runs = list_search_provider_runs(account_id=ctx.account_id)
    assert [run["provider"] for run in runs] == ["bing", "duckduckgo"]
    assert {run["provider"]: run["status"] for run in runs} == {
        "duckduckgo": "failed",
        "bing": "succeeded",
    }
