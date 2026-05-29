import json
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.config import settings


logger = logging.getLogger("ai4all.llm")


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
        return content, None

    if finish_reason == "tool_calls":
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return "", "tool_calls_missing"
        tool_call = tool_calls[0]
        tool_name = tool_call["function"]["name"]
        try:
            tool_args = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            tool_args = {}

        from app.tools.executor import execute_tool_call
        tool_result = execute_tool_call(tool_name, tool_args, ctx)
        tool_result_str = json.dumps(tool_result, ensure_ascii=False)

        messages2 = messages + [
            {"role": "assistant", "tool_calls": [tool_call]},
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", "call_0"),
                "content": tool_result_str,
            },
        ]
        try:
            final_text = _http_chat(messages2)
        except RuntimeError as err:
            return "", str(err)
        return final_text, None

    return "", f"unexpected_finish_reason:{finish_reason}"
