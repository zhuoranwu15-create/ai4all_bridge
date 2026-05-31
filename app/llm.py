import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings


logger = logging.getLogger("ai4all.llm")

# DeepSeek sometimes emits tool calls as DSML text (finish_reason="stop") instead of
# the standard tool_calls JSON field.  These patterns parse that fallback format.
_DSML_INVOKE_RE = re.compile(
    r'<[｜|]{2}DSML[｜|]{2}invoke\s+name=["\']([^"\']+)["\']>',
    re.IGNORECASE,
)
_DSML_PARAM_RE = re.compile(
    r'<[｜|]{2}DSML[｜|]{2}parameter\s+name=["\']([^"\']+)["\'][^>]*>(.*?)</[｜|]{2}DSML[｜|]{2}parameter>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_dsml_tool_call(content: str):
    """Return (tool_name, args_dict) if content contains a DSML tool call, else None."""
    m = _DSML_INVOKE_RE.search(content)
    if not m:
        return None
    tool_name = m.group(1)
    args = {pm.group(1): pm.group(2).strip() for pm in _DSML_PARAM_RE.finditer(content)}
    return tool_name, args


def _fallback_reply(text: str) -> str:
    return f"AI4ALL mock 已收到：{text or '空消息'}"


def _chat_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        settings.llm_timeout_seconds,
        connect=settings.llm_connect_timeout_seconds,
    )


def _chat_transport() -> httpx.HTTPTransport:
    local_address = "0.0.0.0" if settings.llm_force_ipv4 else None
    return httpx.HTTPTransport(
        retries=max(0, int(settings.llm_max_retries)),
        local_address=local_address,
    )


def _http_chat(messages: List[Dict[str, str]]) -> str:
    """Send a messages list to the LLM and return the reply content."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
    }
    max_attempts = max(1, int(settings.llm_max_retries) + 1)
    last_request_error: Optional[httpx.RequestError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(
                timeout=_chat_timeout(),
                trust_env=False,
                transport=_chat_transport(),
            ) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            break
        except httpx.HTTPStatusError as err:
            detail = err.response.text
            status_code = err.response.status_code
            logger.error("llm http error status=%s body=%s", status_code, detail[:500])
            raise RuntimeError(f"LLM HTTP error: {status_code}") from err
        except httpx.RequestError as err:
            last_request_error = err
            logger.warning(
                "llm request failed attempt=%s/%s error=%s",
                attempt,
                max_attempts,
                err,
            )
            if attempt >= max_attempts:
                raise RuntimeError("LLM request failed") from err
            time.sleep(min(0.2 * attempt, 1.0))
        except json.JSONDecodeError as err:
            logger.error("llm invalid json response: %s", err)
            raise RuntimeError("LLM returned invalid JSON") from err
    else:
        raise RuntimeError("LLM request failed") from last_request_error

    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise RuntimeError("LLM returned empty content")
    return content


def generate_reply(
    *,
    user_text: str,
    history: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
) -> str:
    if not settings.llm_api_key:
        return _fallback_reply(user_text)
    prompt = system_prompt or settings.llm_default_prompt
    messages = [{"role": "system", "content": prompt}]
    messages.extend(history)
    return _http_chat(messages)


def generate_completion(messages: List[Dict[str, str]]) -> str:
    """One-shot LLM call with an explicit messages list. Returns '' if no API key."""
    if not settings.llm_api_key:
        return ""
    return _http_chat(messages)


def _http_chat_with_tools(messages: List[Dict], tools: List[Dict]) -> Dict:
    """LLM call with tool definitions. Returns raw response dict."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
        "tools": tools,
        "tool_choice": "auto",
    }
    max_attempts = max(1, int(settings.llm_max_retries) + 1)
    last_request_error: Optional[httpx.RequestError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(
                timeout=_chat_timeout(),
                trust_env=False,
                transport=_chat_transport(),
            ) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as err:
            logger.error("llm http error status=%s body=%s", err.response.status_code, err.response.text[:500])
            raise RuntimeError(f"LLM HTTP error: {err.response.status_code}") from err
        except httpx.RequestError as err:
            last_request_error = err
            logger.warning("llm request failed attempt=%s/%s error=%s", attempt, max_attempts, err)
            if attempt >= max_attempts:
                raise RuntimeError("LLM request failed") from err
            time.sleep(min(0.2 * attempt, 1.0))
    raise RuntimeError("LLM request failed") from last_request_error


def _tool_result_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "succeeded"
    status = str(result.get("status") or "").strip().lower()
    if status in {"queued", "running", "failed"}:
        return status
    if result.get("error"):
        return "failed"
    return "succeeded"


def _record_tool_invocation_start(
    *,
    ctx,
    tool_call_id: Optional[str],
    tool_name: str,
    tool_args: Dict[str, Any],
) -> Optional[int]:
    try:
        from app.db import create_tool_invocation

        invocation = create_tool_invocation(
            account_id=ctx.account_id,
            session_id=int(ctx.session["id"]) if ctx.session.get("id") is not None else None,
            message_id=ctx.message_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=tool_args,
            status="running",
        )
        return int(invocation["id"])
    except Exception as err:
        logger.warning("failed to record tool invocation start tool=%s error=%s", tool_name, err)
        return None


def _record_tool_invocation_finish(
    *,
    tool_invocation_id: Optional[int],
    status: str,
    result: Dict[str, Any],
    latency_ms: int,
    error: Optional[str],
) -> None:
    if tool_invocation_id is None:
        return
    try:
        from app.db import update_tool_invocation

        update_tool_invocation(
            tool_invocation_id=tool_invocation_id,
            status=status,
            result=result,
            latency_ms=latency_ms,
            error=error,
            finished=status in {"queued", "succeeded", "failed"},
        )
    except Exception as err:
        logger.warning(
            "failed to record tool invocation finish id=%s error=%s",
            tool_invocation_id,
            err,
        )


def _execute_and_record_tool_call(tool_call: Dict[str, Any], ctx) -> Dict[str, Any]:
    function = tool_call.get("function") or {}
    tool_name = function.get("name") or ""
    try:
        tool_args = json.loads(function.get("arguments") or "{}")
        if not isinstance(tool_args, dict):
            tool_args = {}
    except (json.JSONDecodeError, TypeError):
        tool_args = {}
    tool_call_id = tool_call.get("id") or "call_0"
    invocation_id = _record_tool_invocation_start(
        ctx=ctx,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=tool_args,
    )
    started = time.monotonic()
    from app.tools.executor import execute_tool_call

    tool_result = execute_tool_call(
        tool_name,
        tool_args,
        ctx,
        tool_call_id=tool_call_id,
        tool_invocation_id=invocation_id,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    status = _tool_result_status(tool_result)
    error = str(tool_result.get("error")) if isinstance(tool_result, dict) and tool_result.get("error") else None
    _record_tool_invocation_finish(
        tool_invocation_id=invocation_id,
        status=status,
        result=tool_result if isinstance(tool_result, dict) else {"result": tool_result},
        latency_ms=latency_ms,
        error=error,
    )
    return tool_result if isinstance(tool_result, dict) else {"result": tool_result}


def _fake_dsml_tool_call(tool_name: str, tool_args: Dict[str, Any], index: int) -> Dict[str, Any]:
    return {
        "id": f"call_dsml_{index}",
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(tool_args, ensure_ascii=False),
        },
    }


def generate_reply_with_tools(
    *,
    user_text: str,
    history: List[Dict],
    system_prompt: Optional[str],
    tools: List[Dict],
    ctx,
) -> tuple:
    """LLM call with tool use support. Returns (reply_text, error_str | None)."""
    if not settings.llm_api_key:
        return _fallback_reply(user_text), None

    prompt = system_prompt or settings.llm_default_prompt
    messages: List[Dict] = [{"role": "system", "content": prompt}]
    messages.extend(history)
    max_tool_rounds = int(getattr(settings, "llm_max_tool_rounds", 3) or 3)
    max_tool_rounds = max(1, min(max_tool_rounds, 8))

    for round_index in range(max_tool_rounds + 1):
        try:
            response = _http_chat_with_tools(messages, tools)
        except RuntimeError as err:
            return "", str(err)

        choice = response.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason", "")
        message = choice.get("message", {})

        if finish_reason in ("stop", "end_turn"):
            content = (message.get("content") or "").strip()
            if not content:
                return "", "llm_empty_response"

            # DeepSeek fallback: tool call encoded as DSML text instead of the standard field.
            dsml = _parse_dsml_tool_call(content)
            if dsml:
                if round_index >= max_tool_rounds:
                    return "", "tool_round_limit_exceeded"
                tool_name, tool_args = dsml
                logger.info("dsml_tool_call detected tool=%s args=%s", tool_name, tool_args)
                fake_tool_call = _fake_dsml_tool_call(tool_name, tool_args, round_index)
                tool_result = _execute_and_record_tool_call(fake_tool_call, ctx)
                messages.extend(
                    [
                        {"role": "assistant", "tool_calls": [fake_tool_call]},
                        {
                            "role": "tool",
                            "tool_call_id": fake_tool_call["id"],
                            "content": json.dumps(tool_result, ensure_ascii=False),
                        },
                    ]
                )
                continue

            return content, None

        if finish_reason == "tool_calls":
            if round_index >= max_tool_rounds:
                return "", "tool_round_limit_exceeded"
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return "", "tool_calls_missing"
            assistant_message = {
                "role": "assistant",
                "tool_calls": tool_calls,
            }
            if message.get("content") is not None:
                assistant_message["content"] = message.get("content")
            messages.append(assistant_message)
            for tool_call in tool_calls:
                tool_result = _execute_and_record_tool_call(tool_call, ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", "call_0"),
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            continue

        return "", f"unexpected_finish_reason:{finish_reason}"

    return "", "tool_round_limit_exceeded"
