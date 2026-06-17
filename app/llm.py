import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings


logger = logging.getLogger("ai4all.llm")
_MOCK_FALLBACK_ENVS = {"local", "development", "test"}

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


def _settings_app_env() -> str:
    env = getattr(settings, "app_env", "local")
    if not isinstance(env, str):
        return "local"
    return env.strip().lower() or "local"


def _llm_api_key() -> str:
    return str(getattr(settings, "llm_api_key", "") or "").strip()


def _message_text_for_mock_fallback(
    user_text: str,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Pick the user-visible text for local mock mode without exposing system prompt."""
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text_parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    part = item.get("text") or item.get("content")
                    if isinstance(part, str) and part.strip():
                        text_parts.append(part.strip())
            if text_parts:
                return "\n".join(text_parts)
    return (user_text or "").strip()


def _fallback_reply(
    text: str,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    fallback_text = _message_text_for_mock_fallback(text, messages)
    return f"AI4ALL mock 已收到：{fallback_text or '空消息'}"


def _missing_api_key_reply(
    user_text: str,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if _settings_app_env() in _MOCK_FALLBACK_ENVS:
        return _fallback_reply(user_text, messages)
    logger.error("llm_api_key missing in env=%s", _settings_app_env())
    raise RuntimeError("LLM API key is missing")


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


def _http_chat_payload(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """POST messages 到 LLM，返回解析后的 JSON payload（含 choices/usage），带重试。"""
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
            return payload
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
    raise RuntimeError("LLM request failed") from last_request_error


def _extract_content(payload: Dict[str, Any]) -> str:
    """从 payload 提取回复正文；空则抛错（与原 _http_chat 行为一致）。"""
    content = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise RuntimeError("LLM returned empty content")
    return content


def _extract_usage(payload: Dict[str, Any]) -> Optional[Dict[str, Optional[int]]]:
    """从 OpenAI 风格 payload 取 token 用量；缺失返回 None（NULL 安全）。"""
    try:
        usage = payload.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return None
        return {"input": prompt, "output": completion}
    except Exception:
        return None


def _http_chat(messages: List[Dict[str, str]]) -> str:
    """Send a messages list to the LLM and return the reply content."""
    return _extract_content(_http_chat_payload(messages))


def _history_with_current_user(
    history: List[Dict[str, str]],
    user_text: str,
) -> List[Dict[str, str]]:
    """Return history plus the current user message when the caller omitted it."""
    messages = list(history)
    text = (user_text or "").strip()
    if not text:
        return messages
    if messages and messages[-1].get("role") == "user":
        return messages
    messages.append({"role": "user", "content": text})
    return messages


def generate_reply(
    *,
    user_text: str,
    history: List[Dict[str, str]],
    system_prompt: Optional[str] = None,
    messages: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not _llm_api_key():
        return _missing_api_key_reply(user_text, messages)
    if messages is not None:
        return _http_chat(messages)
    prompt = system_prompt or settings.llm_default_prompt
    built_messages = [{"role": "system", "content": prompt}]
    built_messages.extend(_history_with_current_user(history, user_text))
    return _http_chat(built_messages)


def generate_completion(messages: List[Dict[str, str]]) -> str:
    """One-shot LLM call with an explicit messages list. Returns '' if no API key."""
    if not settings.llm_api_key:
        return ""
    return _http_chat(messages)


def generate_completion_with_usage(
    messages: List[Dict[str, str]],
) -> Tuple[str, Optional[Dict[str, Optional[int]]]]:
    """同 generate_completion，但额外返回 token 用量 {'input','output'}（缺失为 None）。

    无 API key 时返回 ('', None)。供需要计量 token 的旁路（如 dreaming）使用。
    """
    if not settings.llm_api_key:
        return "", None
    payload = _http_chat_payload(messages)
    return _extract_content(payload), _extract_usage(payload)


def _http_chat_with_tools(messages: List[Dict], tools: List[Dict], *, tool_choice: Any = "auto") -> Dict:
    """LLM call with tool definitions. Returns raw response dict."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
        "tools": tools,
        "tool_choice": tool_choice,
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
        except json.JSONDecodeError as err:
            logger.error("llm invalid json response: %s", err)
            raise RuntimeError("LLM returned invalid JSON") from err
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
    max_tool_rounds: Optional[int] = None,
    first_round_tool_choice: Any = "auto",
    messages: Optional[List[Dict]] = None,
) -> tuple:
    """LLM call with tool use support. Returns (reply_text, error_str | None).

    first_round_tool_choice 由调用方显式传入：第一轮的 tool_choice。默认 "auto"。
    主对话 turn 路径在检测到"主动设置更新"意图时传入强制工具；主动消息生成等
    其它调用方不传（默认 auto），因此不会被内嵌的历史聊天文本误判（曾导致 400）。"""
    if not _llm_api_key():
        try:
            return _missing_api_key_reply(user_text, messages), None
        except RuntimeError as err:
            return "", str(err)

    if messages is None:
        prompt = system_prompt or settings.llm_default_prompt
        tool_messages: List[Dict] = [{"role": "system", "content": prompt}]
        tool_messages.extend(_history_with_current_user(history, user_text))
    else:
        tool_messages = list(messages)
    if max_tool_rounds is None:
        max_tool_rounds = int(getattr(settings, "llm_max_tool_rounds", 3) or 3)
    max_tool_rounds = max(1, min(int(max_tool_rounds), 8))

    # first_round_tool_choice 由调用方决定（见函数 docstring）。
    # 防御：被强制的工具必须确实出现在本次 tools 列表中才生效，否则降级为 "auto"，
    # 避免调用方误传一个本次 tools 不含的工具，导致 deepseek 返回 400
    # "no function named ... in the tools parameter"。
    if isinstance(first_round_tool_choice, dict):
        forced_name = first_round_tool_choice.get("function", {}).get("name")
        available_tool_names = {
            t.get("function", {}).get("name") for t in (tools or [])
        }
        if forced_name not in available_tool_names:
            logger.debug(
                "forced tool_choice=%s not in tools, downgrading to auto", forced_name
            )
            first_round_tool_choice = "auto"

    for round_index in range(max_tool_rounds + 1):
        tc = first_round_tool_choice if round_index == 0 else "auto"
        try:
            response = _http_chat_with_tools(tool_messages, tools, tool_choice=tc)
        except RuntimeError as err:
            return "", str(err)

        choice = response.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason", "")
        message = choice.get("message", {})
        logger.debug(
            "llm_response round=%d finish_reason=%s has_tool_calls=%s content_prefix=%r",
            round_index,
            finish_reason,
            bool(message.get("tool_calls")),
            (message.get("content") or "")[:80],
        )

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
                tool_messages.extend(
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
            tool_messages.append(assistant_message)
            for tool_call in tool_calls:
                tool_result = _execute_and_record_tool_call(tool_call, ctx)
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", "call_0"),
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            continue

        return "", f"unexpected_finish_reason:{finish_reason}"

    return "", "tool_round_limit_exceeded"
