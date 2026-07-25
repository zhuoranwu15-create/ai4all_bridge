#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.platform.search.web import BaiduSearchError, baidu_ai_search  # noqa: E402


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return "***" if text else ""
    return f"{text[:8]}...{text[-6:]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test Baidu AI Search provider.")
    parser.add_argument("query", nargs="?", default="2026 欧冠决赛 比分")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--raw", action="store_true", help="Include raw provider response in output.")
    args = parser.parse_args()

    count = args.count or int(getattr(settings, "web_search_max_results", 5) or 5)
    timeout = args.timeout or float(getattr(settings, "web_search_sync_timeout_seconds", 8.0) or 8.0)

    config = {
        "enabled": bool(getattr(settings, "baidu_ai_search_enabled", False)),
        "base_url": str(getattr(settings, "baidu_ai_search_base_url", "") or ""),
        "endpoint": str(getattr(settings, "baidu_ai_search_endpoint", "") or ""),
        "source": str(getattr(settings, "baidu_ai_search_source", "") or ""),
        "top_k": int(getattr(settings, "baidu_ai_search_top_k", count) or count),
        "api_key": _mask_secret(getattr(settings, "baidu_ai_search_api_key", "") or ""),
    }

    try:
        response = baidu_ai_search(
            query=args.query,
            api_key=str(getattr(settings, "baidu_ai_search_api_key", "") or ""),
            base_url=config["base_url"],
            endpoint=config["endpoint"],
            search_source=config["source"],
            count=count,
            top_k=config["top_k"],
            timeout_seconds=timeout,
            include_raw_response=bool(args.raw),
        )
    except BaiduSearchError as err:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "provider": "baidu",
                    "query": args.query,
                    "config": config,
                    "error": str(err),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "succeeded",
                "provider": response.get("provider"),
                "query": response.get("query"),
                "config": config,
                "latency_ms": response.get("latency_ms"),
                "result_count": len(response.get("results") or []),
                "results": response.get("results") or [],
                "warnings": response.get("warnings") or [],
                "raw_response": response.get("raw_response") if args.raw else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
