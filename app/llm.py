import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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

# Patterns indicating the user wants to UPDATE proactive message settings (not just query).
# Used to force tool_choice on the first LLM round so DeepSeek doesn't ask for confirmation.
_PROACTIVE_COUNT_RE = r"(?:[0-9０-９]+|[一二两三四五六七八九十]+)"
_PROACTIVE_UPDATE_RE = re.compile(
    # frequency: "每天最多1条" / "总共2条" / "一周3次"
    rf"(每天|每日|一天|总共|一周|每周)\s*(最多|最少|只|就)?\s*发?\s*{_PROACTIVE_COUNT_RE}\s*(条|次)"
    # e.g. "条数改为3" / "上限设为2" / "改为3条"
    rf"|(条数|上限|频次).{{0,6}}(改为|设为|调整为|改|设|调|限|调整).{{0,8}}{_PROACTIVE_COUNT_RE}"
    rf"|(改为|设为|调整为|改成|设成).{{0,6}}{_PROACTIVE_COUNT_RE}.{{0,4}}(条|次)"
    rf"|{_PROACTIVE_COUNT_RE}.{{0,5}}(条|次).{{0,8}}(就够|就行|为限|上限|够了)"
    # on/off/mute
    r"|别(再|继续)?(主动|发).{0,10}(消息|找|发)"
    r"|(关掉?|开启?|暂停|停止|恢复).{0,6}主动"
    # action verbs only (exclude noun "设置")
    r"|主动消息.{0,10}(关闭|开启|暂停|停止|修改|调整|改为|设为|限制|减少|增加)"
    r"|(?:主动消息|主动|找我|联系我).{0,8}(?:多|少)发.{0,4}(?:点|些|次|条)"
    r"|(?:多|少)发.{0,4}(?:点|些|次|条).{0,8}(?:主动消息|主动找我|找我|联系我)"
    r"|总(共|量).{0,8}(条数|上限|改为|设为|[0-9０-９])",
    re.IGNORECASE,
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
) -> str:
    if not settings.llm_api_key:
        return _fallback_reply(user_text)
    prompt = system_prompt or settings.llm_default_prompt
    messages = [{"role": "system", "content": prompt}]
    messages.extend(_history_with_current_user(history, user_text))
    return _http_chat(messages)


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


def _infer_proactive_update_tool_choice(messages: List[Dict]) -> Any:
    """Return a forced tool_choice dict if the last user message contains clear
    proactive settings update intent, so DeepSeek doesn't ask for confirmation.
    Returns "auto" otherwise."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = str(msg.get("content") or "")
            if _PROACTIVE_UPDATE_RE.search(content):
                logger.debug("proactive_update_intent detected, forcing tool_choice")
                return {"type": "function", "function": {"name": "update_proactive_message_settings"}}
            break
    return "auto"


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
) -> tuple:
    """LLM call with tool use support. Returns (reply_text, error_str | None)."""
    if not settings.llm_api_key:
        return _fallback_reply(user_text), None

    prompt = system_prompt or settings.llm_default_prompt
    messages: List[Dict] = [{"role": "system", "content": prompt}]
    messages.extend(_history_with_current_user(history, user_text))
    if max_tool_rounds is None:
        max_tool_rounds = int(getattr(settings, "llm_max_tool_rounds", 3) or 3)
    max_tool_rounds = max(1, min(int(max_tool_rounds), 8))

    # On the very first round, detect proactive settings update intent and force
    # the tool to avoid DeepSeek's "let me confirm first" behavior.
    first_round_tool_choice = _infer_proactive_update_tool_choice(messages)

    for round_index in range(max_tool_rounds + 1):
        tc = first_round_tool_choice if round_index == 0 else "auto"
        try:
            response = _http_chat_with_tools(messages, tools, tool_choice=tc)
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
