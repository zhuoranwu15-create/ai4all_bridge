"""Protocol adapters that normalize provider responses to OpenAI chat shape."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.llm_providers import LLMProviderConfig


logger = logging.getLogger("ai4all.llm.adapters")
_VERSION_PATH_RE = re.compile(r"/v\d+(?:/|$)")


def _chat_timeout(provider: LLMProviderConfig) -> httpx.Timeout:
    return httpx.Timeout(
        provider.timeout_seconds,
        connect=provider.connect_timeout_seconds,
    )


def _chat_transport(provider: LLMProviderConfig) -> httpx.HTTPTransport:
    local_address = "0.0.0.0" if provider.force_ipv4 else None
    return httpx.HTTPTransport(
        retries=0,
        local_address=local_address,
    )


def _post_json(
    *,
    provider: LLMProviderConfig,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    max_attempts = max(1, int(provider.max_retries) + 1)
    last_request_error: Optional[httpx.RequestError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(
                timeout=_chat_timeout(provider),
                trust_env=False,
                transport=_chat_transport(provider),
            ) as client:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as err:
            detail = err.response.text
            status_code = err.response.status_code
            logger.error(
                "llm http error provider=%s protocol=%s status=%s body=%s",
                provider.id,
                provider.protocol,
                status_code,
                detail[:500],
            )
            raise RuntimeError(f"LLM HTTP error: {status_code}") from err
        except json.JSONDecodeError as err:
            logger.error("llm invalid json response provider=%s error=%s", provider.id, err)
            raise RuntimeError("LLM returned invalid JSON") from err
        except httpx.RequestError as err:
            last_request_error = err
            logger.warning(
                "llm request failed provider=%s attempt=%s/%s error=%s",
                provider.id,
                attempt,
                max_attempts,
                err,
            )
            if attempt >= max_attempts:
                raise RuntimeError("LLM request failed") from err
            time.sleep(min(0.2 * attempt, 1.0))
    raise RuntimeError("LLM request failed") from last_request_error


def _join_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(suffix):
        return base
    return base + suffix


def _openai_chat_url(provider: LLMProviderConfig) -> str:
    return _join_url(provider.base_url, "/chat/completions")


def _openai_responses_url(provider: LLMProviderConfig) -> str:
    return _versioned_endpoint_url(provider.base_url, "/responses")


def _anthropic_messages_url(provider: LLMProviderConfig) -> str:
    return _versioned_endpoint_url(provider.base_url, "/messages")


def _versioned_endpoint_url(base_url: str, endpoint: str) -> str:
    base = base_url.rstrip("/")
    clean_endpoint = "/" + endpoint.strip("/")
    if base.endswith(clean_endpoint):
        return base
    if _VERSION_PATH_RE.search(urlsplit(base).path.rstrip("/")):
        return base + clean_endpoint
    return base + "/v1" + clean_endpoint


def _openai_headers(provider: LLMProviderConfig) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }


def _anthropic_headers(provider: LLMProviderConfig) -> Dict[str, str]:
    return {
        "x-api-key": provider.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


def _openai_chat_payload(
    provider: LLMProviderConfig,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
) -> Dict[str, Any]:
    clean_messages = [
        {key: value for key, value in message.items() if not str(key).startswith("_")}
        for message in messages
    ]
    body: Dict[str, Any] = {
        "model": provider.model,
        "messages": clean_messages,
        "temperature": provider.temperature,
    }
    if tools is not None:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    return body


def _responses_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("content")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def _responses_input_from_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").strip()
        if role == "system":
            role = "developer"
        if role in {"developer", "user", "assistant"}:
            tool_calls = message.get("tool_calls") or []
            content_text = _responses_content_text(message.get("content"))
            if content_text:
                items.append({"role": role, "content": content_text})
            elif role != "assistant" or not tool_calls:
                items.append({"role": role, "content": ""})
            for call in tool_calls:
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or "call_0",
                        "name": function.get("name") or "",
                        "arguments": function.get("arguments") or "{}",
                    }
                )
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "call_0",
                    "output": _responses_content_text(message.get("content")),
                }
            )
    return items


def _responses_input_and_previous_id(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    previous_response_id = None
    start_index = 0
    for index, message in enumerate(messages):
        value = message.get("_provider_response_id")
        if isinstance(value, str) and value.strip():
            previous_response_id = value.strip()
            start_index = index + 1
    if previous_response_id:
        followup_items = _responses_input_from_messages(messages[start_index:])
        if followup_items:
            return followup_items, previous_response_id
    return _responses_input_from_messages(messages), None


def _responses_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _responses_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"type": "function", "name": name}
    if tool_choice in {"auto", "none", "required"}:
        return tool_choice
    return "auto"


def _openai_responses_body(
    provider: LLMProviderConfig,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
) -> Dict[str, Any]:
    input_items, previous_response_id = _responses_input_and_previous_id(messages)
    body: Dict[str, Any] = {
        "model": provider.model,
        "input": input_items,
        "temperature": provider.temperature,
        "max_output_tokens": provider.max_output_tokens,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    if tools is not None:
        body["tools"] = _responses_tools(tools)
        body["tool_choice"] = _responses_tool_choice(tool_choice)
    return body


def _normalize_openai_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    tool_calls: List[Dict[str, Any]] = []
    content_chunks: List[str] = []
    output_text = payload.get("output_text")
    has_output_text = isinstance(output_text, str) and bool(output_text)
    if has_output_text:
        content_chunks.append(output_text)
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            if has_output_text:
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text:
                    content_chunks.append(text)
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
    usage = payload.get("usage") or {}
    normalized_usage = {
        "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
    }
    message: Dict[str, Any] = {"role": "assistant", "content": "\n".join(content_chunks).strip()}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None
        finish_reason = "tool_calls"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": normalized_usage,
        "id": payload.get("id"),
        "_provider_protocol": "openai_responses",
    }


def _anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") or {}
        out.append(
            {
                "name": function.get("name"),
                "description": function.get("description") or "",
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _anthropic_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text:
                    chunks.append(text)
        return "\n".join(chunks)
    return ""


def _anthropic_content_block_from_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_call.get("function") or {}
    args = function.get("arguments") or "{}"
    try:
        parsed_args = json.loads(args)
        if not isinstance(parsed_args, dict):
            parsed_args = {}
    except (TypeError, json.JSONDecodeError):
        parsed_args = {}
    return {
        "type": "tool_use",
        "id": tool_call.get("id") or "call_0",
        "name": function.get("name") or "",
        "input": parsed_args,
    }


def _anthropic_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    system_chunks: List[str] = []
    out: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").strip()
        if role == "system":
            text = _message_text(message.get("content"))
            if text:
                system_chunks.append(text)
            continue
        if role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id") or "call_0",
                            "content": _message_text(message.get("content")),
                        }
                    ],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            continue
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            blocks: List[Dict[str, Any]] = []
            text = _message_text(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            blocks.extend(_anthropic_content_block_from_tool_call(call) for call in tool_calls)
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": role, "content": _message_text(message.get("content"))})
    return "\n\n".join(system_chunks), out


def _anthropic_body(
    provider: LLMProviderConfig,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
) -> Dict[str, Any]:
    system, anthropic_messages = _anthropic_messages(messages)
    body: Dict[str, Any] = {
        "model": provider.model,
        "max_tokens": provider.max_output_tokens,
        "messages": anthropic_messages,
        "temperature": provider.temperature,
    }
    if system:
        body["system"] = system
    if tools is not None:
        body["tools"] = _anthropic_tools(tools)
        converted_choice = _anthropic_tool_choice(tool_choice)
        if converted_choice:
            body["tool_choice"] = converted_choice
    return body


def _normalize_anthropic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    content_chunks: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                content_chunks.append(text)
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    usage = payload.get("usage") or {}
    normalized_usage = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
    }
    message: Dict[str, Any] = {"role": "assistant", "content": "\n".join(content_chunks).strip()}
    finish_reason = "stop"
    if tool_calls or payload.get("stop_reason") == "tool_use":
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None
        finish_reason = "tool_calls"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": normalized_usage,
        "id": payload.get("id"),
        "_provider_protocol": "anthropic_messages",
    }


# DeepSeek sometimes emits tool calls as DSML text (finish_reason="stop") instead of
# the standard tool_calls JSON field. These patterns parse that fallback format.
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


def _normalize_openai_chat_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize openai_chat response, handling DeepSeek DSML tool-call fallback.

    Converts DSML-encoded tool calls to the standard tool_calls JSON field so the
    orchestration layer stays provider-agnostic.
    """
    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError):
        return payload
    if choice.get("finish_reason") != "stop":
        return payload
    message = choice.get("message", {})
    if message.get("tool_calls"):
        return payload
    content = (message.get("content") or "").strip()
    dsml = _parse_dsml_tool_call(content)
    if not dsml:
        return payload
    tool_name, tool_args = dsml
    logger.info("dsml_tool_call detected in adapter tool=%s", tool_name)
    message["tool_calls"] = [
        {
            "id": "call_dsml_0",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_args, ensure_ascii=False),
            },
        }
    ]
    message["content"] = None
    choice["finish_reason"] = "tool_calls"
    return payload


def chat_completion(
    provider: LLMProviderConfig,
    messages: List[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
) -> Dict[str, Any]:
    """Call the provider and return an OpenAI chat-compatible response payload."""
    if provider.protocol == "openai_chat":
        payload = _post_json(
            provider=provider,
            url=_openai_chat_url(provider),
            headers=_openai_headers(provider),
            body=_openai_chat_payload(provider, messages, tools=tools, tool_choice=tool_choice),
        )
        payload["_provider_protocol"] = "openai_chat"
        return _normalize_openai_chat_payload(payload)
    if provider.protocol == "openai_responses":
        payload = _post_json(
            provider=provider,
            url=_openai_responses_url(provider),
            headers=_openai_headers(provider),
            body=_openai_responses_body(provider, messages, tools=tools, tool_choice=tool_choice),
        )
        return _normalize_openai_responses_payload(payload)
    if provider.protocol == "anthropic_messages":
        payload = _post_json(
            provider=provider,
            url=_anthropic_messages_url(provider),
            headers=_anthropic_headers(provider),
            body=_anthropic_body(provider, messages, tools=tools, tool_choice=tool_choice),
        )
        return _normalize_anthropic_payload(payload)
    raise RuntimeError(f"unsupported LLM protocol: {provider.protocol}")
