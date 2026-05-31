#!/usr/bin/env python3
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


DEFAULT_ENDPOINT = "https://cloud-iqs.aliyuncs.com/search/unified"


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return "***" if text else ""
    return f"{text[:8]}...{text[-6:]}"


def _collapse_ws(value: Any) -> str:
    return " ".join(str(value or "").split())


def _iter_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _extract_results(payload: Dict[str, Any], *, limit: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in _iter_dicts(payload):
        url = _collapse_ws(
            item.get("url")
            or item.get("link")
            or item.get("pageUrl")
            or item.get("page_url")
            or item.get("sourceUrl")
            or item.get("source_url")
            or ""
        )
        title = _collapse_ws(item.get("title") or item.get("name") or item.get("siteName") or url)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": _collapse_ws(
                    item.get("summary")
                    or item.get("mainText")
                    or item.get("markdownText")
                    or item.get("snippet")
                    or item.get("content")
                    or ""
                )[:800],
                "rerank_score": item.get("rerankScore") or item.get("rerank_score"),
            }
        )
        if len(results) >= limit:
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test Aliyun Cloud IQS unified search.")
    parser.add_argument("query", nargs="?", default="杭州美食")
    parser.add_argument("--endpoint", default=str(getattr(settings, "aliyun_web_search_base_url", "") or DEFAULT_ENDPOINT))
    parser.add_argument("--engine-type", default=str(getattr(settings, "aliyun_web_search_engine_type", "") or "LiteAdvanced"))
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--raw", action="store_true", help="Include full raw response in output.")
    args = parser.parse_args()

    api_key = args.api_key or str(
        getattr(settings, "aliyun_web_search_api_key", "")
        or getattr(settings, "dashscope_api_key", "")
        or ""
    )
    body = {
        "query": args.query,
        "engineType": args.engine_type,
        "contents": {
            "mainText": True,
            "markdownText": False,
            "summary": False,
            "rerankScore": True,
        },
        "advancedParams": {
            "numResults": max(1, min(int(args.count or 5), 20)),
        },
    }

    started = time.monotonic()
    try:
        with httpx.Client(timeout=args.timeout, follow_redirects=True, trust_env=False) as client:
            response = client.post(
                args.endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw_text": response.text}
    except httpx.HTTPError as err:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "provider": "aliyun_iqs",
                    "endpoint": args.endpoint,
                    "query": args.query,
                    "api_key": _mask_secret(api_key),
                    "error": str(err),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    output: Dict[str, Any] = {
        "status": "succeeded" if 200 <= response.status_code < 300 else "failed",
        "provider": "aliyun_iqs",
        "endpoint": args.endpoint,
        "query": args.query,
        "engine_type": args.engine_type,
        "api_key": _mask_secret(api_key),
        "http_status": response.status_code,
        "latency_ms": latency_ms,
        "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "results": _extract_results(payload, limit=args.count) if isinstance(payload, dict) else [],
    }
    if not 200 <= response.status_code < 300 and isinstance(payload, dict):
        output["error_body"] = payload
    if args.raw:
        output["raw_response"] = payload

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if 200 <= response.status_code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
