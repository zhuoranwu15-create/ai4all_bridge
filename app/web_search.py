import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree

import httpx


class DuckDuckGoSearchError(RuntimeError):
    pass


class BingSearchError(RuntimeError):
    pass


class AliyunSearchError(RuntimeError):
    pass


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None
        self._capture: Optional[str] = None
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set((attr.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._current = {
                "title": "",
                "url": _normalize_duckduckgo_url(attr.get("href") or ""),
                "snippet": "",
            }
            self._capture = "title"
            self._parts = []
            return
        if self._current is not None and "result__snippet" in classes:
            self._capture = "snippet"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None or self._capture is None:
            return
        if self._capture == "title" and tag == "a":
            self._current["title"] = _collapse_ws("".join(self._parts))
            if self._current.get("title") and self._current.get("url"):
                self.results.append(self._current)
            self._capture = None
            self._parts = []
            return
        if self._capture == "snippet" and tag in {"a", "td", "div"}:
            snippet = _collapse_ws("".join(self._parts))
            if snippet:
                self._current["snippet"] = snippet
            self._capture = None
            self._parts = []


def _collapse_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def _normalize_duckduckgo_url(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urlparse(raw)
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    return raw


def _site_name(url: str) -> Optional[str]:
    host = urlparse(url or "").netloc
    return host or None


def _citations(results: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [
        {"title": item.get("title") or item.get("url") or "", "url": item.get("url") or ""}
        for item in results
        if item.get("title") or item.get("url")
    ]


def _search_response(
    *,
    provider: str,
    query: str,
    results: List[Dict[str, Any]],
    latency_ms: int,
    warnings: Optional[List[Dict[str, Any]]] = None,
    answer: Optional[str] = None,
    raw_response: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = {
        "provider": provider,
        "query": query,
        "results": results,
        "citations": _citations(results),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": latency_ms,
        "warnings": warnings or [],
    }
    if answer:
        response["answer"] = answer
    if raw_response is not None:
        response["raw_response"] = raw_response
    return response


def _unsupported_filter_warnings(provider: str, fields: List[str]) -> List[Dict[str, Any]]:
    if not fields:
        return []
    return [
        {
            "code": "unsupported_filter",
            "fields": fields,
            "message": f"{provider} adapter does not support these filters yet; search ran without them.",
        }
    ]


def _payload_error(payload: Dict[str, Any]) -> Optional[str]:
    error = payload.get("error")
    if isinstance(error, dict):
        code = _collapse_ws(error.get("code") or error.get("type") or "")
        message = _collapse_ws(error.get("message") or "")
        if code and message:
            return f"{code}: {message}"
        return code or message or None

    code = _collapse_ws(payload.get("code") or payload.get("error_code") or "")
    message = _collapse_ws(payload.get("message") or payload.get("error_msg") or payload.get("msg") or "")
    if code and message:
        return f"{code}: {message}"
    return code or message or None


def _http_status_error_message(provider: str, response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = _payload_error(payload)
        if detail:
            return f"{provider} error {detail} (HTTP {response.status_code})"

    text = _collapse_ws(response.text or "")
    if text:
        return f"{provider} request failed: HTTP {response.status_code}: {text[:500]}"
    return f"{provider} request failed: HTTP {response.status_code}"


def parse_duckduckgo_html(html: str, *, count: int) -> List[Dict[str, Any]]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html or "")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    seen: set[str] = set()
    results: List[Dict[str, Any]] = []
    for item in parser.results:
        url = item.get("url") or ""
        title = item.get("title") or ""
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": item.get("snippet") or "",
                "site_name": _site_name(url),
                "retrieved_at": retrieved_at,
                "score": None,
            }
        )
        if len(results) >= count:
            break
    return results


def duckduckgo_search(
    *,
    query: str,
    count: int = 5,
    timeout_seconds: float = 8.0,
    language: Optional[str] = None,
    country: Optional[str] = None,
    freshness: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> Dict[str, Any]:
    cleaned_query = _collapse_ws(query)
    if not cleaned_query:
        raise DuckDuckGoSearchError("query is required")
    normalized_count = min(max(int(count or 5), 1), 10)
    unsupported_filters = [
        name
        for name, value in (
            ("freshness", freshness),
            ("date_after", date_after),
            ("date_before", date_before),
        )
        if value
    ]
    params: Dict[str, Any] = {"q": cleaned_query}
    if country:
        params["kl"] = str(country).lower()
    if language:
        params["kad"] = str(language).lower()

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, trust_env=False) as client:
            response = client.post(
                "https://html.duckduckgo.com/html/",
                data=params,
                headers={
                    "User-Agent": "AI4ALLBot/0.1 (+https://ai4all.local)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as err:
        raise DuckDuckGoSearchError(f"duckduckgo request failed: {err}") from err

    latency_ms = int((time.monotonic() - started) * 1000)
    results = parse_duckduckgo_html(response.text, count=normalized_count)
    if not results:
        raise DuckDuckGoSearchError("duckduckgo returned no results")

    return _search_response(
        provider="duckduckgo",
        query=cleaned_query,
        results=results,
        latency_ms=latency_ms,
        warnings=_unsupported_filter_warnings("DuckDuckGo", unsupported_filters),
    )


def parse_bing_rss(xml_text: str, *, count: int) -> List[Dict[str, Any]]:
    try:
        root = ElementTree.fromstring(xml_text or "")
    except ElementTree.ParseError as err:
        raise BingSearchError("bing returned invalid rss") from err

    retrieved_at = datetime.now(timezone.utc).isoformat()
    seen: set[str] = set()
    results: List[Dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = _collapse_ws(item.findtext("title") or "")
        url = _collapse_ws(item.findtext("link") or "")
        snippet = _collapse_ws(item.findtext("description") or "")
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "site_name": _site_name(url),
                "retrieved_at": retrieved_at,
                "score": None,
            }
        )
        if len(results) >= count:
            break
    return results


def bing_search(
    *,
    query: str,
    count: int = 5,
    timeout_seconds: float = 8.0,
    language: Optional[str] = None,
    country: Optional[str] = None,
    freshness: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
) -> Dict[str, Any]:
    cleaned_query = _collapse_ws(query)
    if not cleaned_query:
        raise BingSearchError("query is required")
    normalized_count = min(max(int(count or 5), 1), 10)
    unsupported_filters = [
        name
        for name, value in (
            ("freshness", freshness),
            ("date_after", date_after),
            ("date_before", date_before),
            ("country", country),
        )
        if value
    ]
    params: Dict[str, Any] = {"q": cleaned_query, "format": "rss"}
    if language:
        params["setlang"] = str(language).lower()

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, trust_env=False) as client:
            response = client.get(
                "https://cn.bing.com/search",
                params=params,
                headers={
                    "User-Agent": "AI4ALLBot/0.1 (+https://ai4all.local)",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as err:
        raise BingSearchError(f"bing request failed: {err}") from err

    latency_ms = int((time.monotonic() - started) * 1000)
    results = parse_bing_rss(response.text, count=normalized_count)
    if not results:
        raise BingSearchError("bing returned no results")

    return _search_response(
        provider="bing",
        query=cleaned_query,
        results=results,
        latency_ms=latency_ms,
        warnings=_unsupported_filter_warnings("Bing RSS", unsupported_filters),
    )


def _extract_aliyun_answer(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return _collapse_ws(content)

    output = payload.get("output")
    if isinstance(output, dict):
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return _collapse_ws(content)
        text = output.get("text")
        if isinstance(text, str):
            return _collapse_ws(text)
    return ""


def _iter_reference_candidates(node: Any):
    reference_keys = {
        "references",
        "pageItems",
        "search_results",
        "searchResults",
        "search_results_list",
        "docs",
        "documents",
        "sources",
    }
    if isinstance(node, dict):
        for key, value in node.items():
            if key in reference_keys and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
            else:
                yield from _iter_reference_candidates(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_reference_candidates(item)


def parse_aliyun_web_search_response(payload: Dict[str, Any], *, count: int) -> List[Dict[str, Any]]:
    retrieved_at = datetime.now(timezone.utc).isoformat()
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in _iter_reference_candidates(payload):
        url = _collapse_ws(
            item.get("url")
            or item.get("link")
            or item.get("source_url")
            or item.get("page_url")
            or ""
        )
        title = _collapse_ws(
            item.get("title")
            or item.get("name")
            or item.get("site_name")
            or item.get("hostname")
            or url
        )
        snippet = _collapse_ws(
            item.get("snippet")
            or item.get("content")
            or item.get("summary")
            or item.get("mainText")
            or item.get("markdownText")
            or item.get("text")
            or ""
        )
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "site_name": _collapse_ws(
                    item.get("site_name")
                    or item.get("siteName")
                    or item.get("hostname")
                    or item.get("hostName")
                    or ""
                ) or _site_name(url),
                "retrieved_at": retrieved_at,
                "score": item.get("score") or item.get("rerankScore") or item.get("rerank_score"),
                "published_at": item.get("date") or item.get("publishedTime") or item.get("published_at"),
            }
        )
        if len(results) >= count:
            break
    return results


def aliyun_web_search(
    *,
    query: str,
    api_key: str,
    base_url: str = "https://cloud-iqs.aliyuncs.com/search/unified",
    engine_type: str = "LiteAdvanced",
    model: str = "qwen-plus",
    count: int = 5,
    timeout_seconds: float = 8.0,
    forced: bool = True,
    enable_source: bool = True,
    search_strategy: Optional[str] = None,
    language: Optional[str] = None,
    country: Optional[str] = None,
    freshness: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_raw_response: bool = False,
) -> Dict[str, Any]:
    cleaned_query = _collapse_ws(query)
    if not cleaned_query:
        raise AliyunSearchError("query is required")
    if not api_key:
        raise AliyunSearchError("aliyun api key is required")
    normalized_count = min(max(int(count or 5), 1), 10)
    unsupported_filters = [
        name
        for name, value in (
            ("language", language),
            ("country", country),
            ("freshness", freshness),
            ("date_after", date_after),
            ("date_before", date_before),
        )
        if value
    ]
    body: Dict[str, Any] = {
        "query": cleaned_query,
        "engineType": _collapse_ws(engine_type) or "LiteAdvanced",
        "contents": {
            "mainText": True,
            "markdownText": False,
            "summary": False,
            "rerankScore": True,
        },
        "advancedParams": {
            "numResults": normalized_count,
        },
    }

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, trust_env=False) as client:
            response = client.post(
                base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as err:
        raise AliyunSearchError(_http_status_error_message("aliyun", err.response)) from err
    except httpx.HTTPError as err:
        raise AliyunSearchError(f"aliyun request failed: {err}") from err
    except ValueError as err:
        raise AliyunSearchError("aliyun returned invalid json") from err

    payload_error = _payload_error(payload)
    if payload_error and (payload.get("error") or not payload.get("choices") and not payload.get("output")):
        raise AliyunSearchError(f"aliyun error {payload_error}")
    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _extract_aliyun_answer(payload)
    results = parse_aliyun_web_search_response(payload, count=normalized_count)
    if not results and answer:
        results = [
            {
                "title": "Aliyun web search answer",
                "url": "",
                "snippet": answer,
                "site_name": "cloud-iqs.aliyuncs.com",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "score": None,
            }
        ]
    if not results:
        raise AliyunSearchError("aliyun returned no results")

    return _search_response(
        provider="aliyun",
        query=cleaned_query,
        results=results,
        latency_ms=latency_ms,
        warnings=_unsupported_filter_warnings("Aliyun Web Search", unsupported_filters),
        answer=answer or None,
        raw_response=payload if include_raw_response else None,
    )
