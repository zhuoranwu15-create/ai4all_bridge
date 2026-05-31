import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from app.config import settings
from app.db import create_search_provider_run
from app.web_search import (
    AliyunSearchError,
    BaiduSearchError,
    BingSearchError,
    DuckDuckGoSearchError,
    aliyun_web_search,
    baidu_ai_search,
    bing_search,
    duckduckgo_search,
)

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.web_search")
_SUPPORTED_PROVIDERS = {"aliyun", "baidu", "bing", "duckduckgo"}
_SEARCH_ERRORS = (AliyunSearchError, BaiduSearchError, BingSearchError, DuckDuckGoSearchError)
_PROVIDER_ORDER_OVERRIDE: ContextVar[Optional[List[str]]] = ContextVar(
    "web_search_provider_order_override",
    default=None,
)


@contextmanager
def override_provider_order(providers: Optional[List[str]]):
    normalized = [str(item).strip().lower() for item in providers or [] if str(item).strip()]
    token = _PROVIDER_ORDER_OVERRIDE.set(normalized or None)
    try:
        yield
    finally:
        _PROVIDER_ORDER_OVERRIDE.reset(token)


def _int_arg(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def handle_web_search(
    args: dict,
    ctx: "TurnContext",
    *,
    tool_call_id: Optional[str] = None,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"status": "failed", "error": "query is required"}

    count = _int_arg(
        args.get("count"),
        int(getattr(settings, "web_search_max_results", 5) or 5),
        min_value=1,
        max_value=10,
    )
    request: Dict[str, Any] = {
        "query": query,
        "count": count,
        "freshness": args.get("freshness"),
        "date_after": args.get("date_after"),
        "date_before": args.get("date_before"),
        "language": args.get("language"),
        "country": args.get("country"),
        "tool_call_id": tool_call_id,
    }

    attempts: List[Dict[str, Any]] = []
    providers = _provider_order()
    failover = bool(getattr(settings, "web_search_provider_failover", True))

    for attempt_index, provider in enumerate(providers, start=1):
        provider_request = {**request, "provider": provider, "attempt": attempt_index}
        if provider not in _SUPPORTED_PROVIDERS:
            error = f"unsupported web_search provider: {provider}"
            _record_provider_run(
                ctx=ctx,
                tool_invocation_id=tool_invocation_id,
                provider=provider,
                request=provider_request,
                status="failed",
                error=error,
                attempt=attempt_index,
            )
            attempts.append({"provider": provider, "status": "failed", "error": error})
            if failover:
                continue
            return _failed_result(query=query, provider=provider, error=error, attempts=attempts)

        try:
            response = _execute_provider(provider, query=query, count=count, args=args)
        except _SEARCH_ERRORS as err:
            error = str(err)
            _record_provider_run(
                ctx=ctx,
                tool_invocation_id=tool_invocation_id,
                provider=provider,
                request=provider_request,
                status="failed",
                error=error,
                attempt=attempt_index,
            )
            attempts.append({"provider": provider, "status": "failed", "error": error})
            if failover:
                continue
            return _failed_result(query=query, provider=provider, error=error, attempts=attempts)

        _record_provider_run(
            ctx=ctx,
            tool_invocation_id=tool_invocation_id,
            provider=provider,
            request=provider_request,
            status="succeeded",
            response=response,
            latency_ms=response.get("latency_ms"),
            attempt=attempt_index,
        )
        return {"status": "succeeded", "attempts": attempts, **response}

    error = attempts[-1]["error"] if attempts else "no web_search provider configured"
    provider = attempts[-1]["provider"] if attempts else ""
    return _failed_result(query=query, provider=provider, error=error, attempts=attempts)


def _provider_order() -> List[str]:
    override = _PROVIDER_ORDER_OVERRIDE.get()
    if override:
        return override
    raw_order = str(getattr(settings, "web_search_provider_order", "") or "").strip()
    if raw_order:
        providers = [item.strip().lower() for item in raw_order.split(",") if item.strip()]
    else:
        providers = [str(getattr(settings, "web_search_default_provider", "duckduckgo") or "duckduckgo").strip().lower()]
    deduped: List[str] = []
    for provider in providers:
        if provider and provider not in deduped:
            deduped.append(provider)
    return deduped or ["duckduckgo"]


def _execute_provider(provider: str, *, query: str, count: int, args: Dict[str, Any]) -> Dict[str, Any]:
    timeout_seconds = float(getattr(settings, "web_search_sync_timeout_seconds", 8.0) or 8.0)
    common: Dict[str, Any] = {
        "query": query,
        "count": count,
        "timeout_seconds": timeout_seconds,
        "freshness": args.get("freshness"),
        "date_after": args.get("date_after"),
        "date_before": args.get("date_before"),
        "language": args.get("language"),
        "country": args.get("country"),
    }
    providers: Dict[str, Callable[[], Dict[str, Any]]] = {
        "duckduckgo": lambda: duckduckgo_search(**common),
        "bing": lambda: bing_search(**common),
        "aliyun": lambda: aliyun_web_search(
            **common,
            api_key=str(
                getattr(settings, "aliyun_web_search_api_key", "")
                or getattr(settings, "dashscope_api_key", "")
                or ""
            ),
            base_url=str(getattr(settings, "aliyun_web_search_base_url", "") or ""),
            engine_type=str(getattr(settings, "aliyun_web_search_engine_type", "") or "LiteAdvanced"),
            model=str(getattr(settings, "aliyun_web_search_model", "") or "qwen-plus"),
            forced=bool(getattr(settings, "aliyun_web_search_forced", True)),
            enable_source=bool(getattr(settings, "aliyun_web_search_enable_source", True)),
            search_strategy=str(getattr(settings, "aliyun_web_search_strategy", "") or "") or None,
            include_raw_response=bool(getattr(settings, "web_search_trace_raw_response", False)),
        ),
        "baidu": lambda: baidu_ai_search(
            **common,
            api_key=str(getattr(settings, "baidu_ai_search_api_key", "") or ""),
            base_url=str(getattr(settings, "baidu_ai_search_base_url", "") or ""),
            endpoint=str(getattr(settings, "baidu_ai_search_endpoint", "") or ""),
            search_source=str(getattr(settings, "baidu_ai_search_source", "") or "baidu_search_v2"),
            top_k=int(getattr(settings, "baidu_ai_search_top_k", count) or count),
            include_raw_response=bool(getattr(settings, "web_search_trace_raw_response", False)),
        ),
    }
    if provider == "aliyun" and not bool(getattr(settings, "aliyun_web_search_enabled", False)):
        raise AliyunSearchError("aliyun web search is disabled")
    if provider == "baidu" and not bool(getattr(settings, "baidu_ai_search_enabled", False)):
        raise BaiduSearchError("baidu ai search is disabled")
    return providers[provider]()


def _failed_result(*, query: str, provider: str, error: str, attempts: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "failed",
        "query": query,
        "provider": provider,
        "error": error,
        "attempts": attempts,
    }


def _record_provider_run(
    *,
    ctx: "TurnContext",
    tool_invocation_id: Optional[int],
    provider: str,
    request: Dict[str, Any],
    status: str,
    response: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    attempt: int = 1,
) -> None:
    try:
        create_search_provider_run(
            account_id=ctx.account_id,
            tool_invocation_id=tool_invocation_id,
            provider=provider,
            attempt=attempt,
            status=status,
            request=request,
            response=response or {},
            latency_ms=latency_ms,
            error=error,
            finished=status in {"succeeded", "failed"},
        )
    except Exception as err:
        logger.warning("failed to record search provider run: %s", err)
