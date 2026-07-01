import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from app.config import settings
from app.db import create_search_provider_run
from app.web_search import (
    AliyunSearchError,
    BingSearchError,
    DuckDuckGoSearchError,
    aliyun_web_search,
    bing_search,
    duckduckgo_search,
)

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.web_search")
_SUPPORTED_PROVIDERS = {"aliyun", "bing", "duckduckgo"}
_SEARCH_ERRORS = (AliyunSearchError, BingSearchError, DuckDuckGoSearchError)
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


def run_headless_web_search(
    query: str,
    *,
    count: int = 5,
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """无 TurnContext 的后台 web 搜索（供全局召回等无账号场景用）。

    复用 provider 顺序 + failover，但**不落 search_provider_runs**（无账号维度可归属）。
    返回与 handle_web_search 同形：成功 {"status":"succeeded", "results":[...], ...}，
    失败 {"status":"failed", "error":...}。
    """
    query = str(query or "").strip()
    if not query:
        return {"status": "failed", "query": query, "provider": "", "error": "query is required", "attempts": []}
    call_args = dict(args or {})
    providers = _provider_order()
    failover = bool(getattr(settings, "web_search_provider_failover", True))
    attempts: List[Dict[str, Any]] = []
    for provider in providers:
        if provider not in _SUPPORTED_PROVIDERS:
            error = f"unsupported web_search provider: {provider}"
            attempts.append({"provider": provider, "status": "failed", "error": error})
            if failover:
                continue
            return _failed_result(query=query, provider=provider, error=error, attempts=attempts)
        try:
            response = _execute_provider(provider, query=query, count=count, args=call_args)
        except _SEARCH_ERRORS as err:
            error = str(err)
            attempts.append({"provider": provider, "status": "failed", "error": error})
            if failover:
                continue
            return _failed_result(query=query, provider=provider, error=error, attempts=attempts)
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
    }
    if provider == "aliyun" and not bool(getattr(settings, "aliyun_web_search_enabled", False)):
        raise AliyunSearchError("aliyun web search is disabled")

    # 每次供应商调用都打点延时日志：定位"带搜索的轮次为何变慢/超时"用，成功失败都记。
    started = time.monotonic()
    try:
        result = providers[provider]()
    except Exception as err:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "web_search call provider=%s status=failed latency_ms=%s timeout_s=%s query=%r error=%s",
            provider,
            elapsed_ms,
            timeout_seconds,
            query[:80],
            err,
        )
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "web_search call provider=%s status=ok latency_ms=%s timeout_s=%s results=%s query=%r",
        provider,
        elapsed_ms,
        timeout_seconds,
        len(result.get("results") or []),
        query[:80],
    )
    return result


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
