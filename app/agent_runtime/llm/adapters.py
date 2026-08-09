"""Protocol adapters that normalize provider responses to OpenAI chat shape."""
from __future__ import annotations

import codecs
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.agent_runtime.llm.providers import LLMProviderConfig


logger = logging.getLogger("ai4all.llm.adapters")
_VERSION_PATH_RE = re.compile(r"/v\d+(?:/|$)")
_DUMP_SEQ_LOCK = threading.Lock()
_DUMP_SEQ = 0


@dataclass(frozen=True)
class LLMStreamEvent:
    """Provider-neutral event emitted by a streaming LLM call."""

    kind: str
    text: str = ""
    tool_call: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    error_code: Optional[str] = None


def _json_body_bytes(body: Dict[str, Any]) -> bytes:
    """Encode JSON exactly once so the dumped bytes are the bytes sent on the wire."""
    return json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _next_dump_seq() -> int:
    global _DUMP_SEQ
    with _DUMP_SEQ_LOCK:
        _DUMP_SEQ += 1
        return _DUMP_SEQ


def _safe_label(value: Any) -> str:
    text = str(value or "unknown").strip()
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    return "".join(cleaned).strip("_")[:80] or "unknown"


def _write_request_dump(
    *,
    provider: LLMProviderConfig,
    url: str,
    body_bytes: bytes,
    attempt: int,
) -> None:
    """Write the exact LLM request body bytes and metadata for local debugging."""
    if not bool(getattr(settings, "llm_request_dump_enabled", False)):
        return

    try:
        dump_dir = Path(str(getattr(settings, "llm_request_dump_dir", "") or "tmp/llm_request_bodies/ai4all"))
        dump_dir.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now().isoformat(timespec="microseconds")
        timestamp = created_at.replace(":", "").replace(".", "").replace("-", "")
        seq = _next_dump_seq()
        digest = hashlib.sha256(body_bytes).hexdigest()
        stem = (
            f"{timestamp}-ai4all-{seq:06d}-"
            f"{_safe_label(provider.id)}-{_safe_label(provider.protocol)}-attempt{attempt}"
        )
        body_path = dump_dir / f"{stem}.body.json"
        meta_path = dump_dir / f"{stem}.meta.json"
        body_path.write_bytes(body_bytes)
        meta = {
            "id": str(uuid.uuid4()),
            "source": "ai4all",
            "created_at": created_at,
            "url": url,
            "method": "POST",
            "provider_id": provider.id,
            "protocol": provider.protocol,
            "model": provider.model,
            "attempt": attempt,
            "body_file": str(body_path),
            "body_bytes": len(body_bytes),
            "body_sha256": digest,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as err:
        logger.warning("failed to dump llm request body provider=%s error=%s", provider.id, err)


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


def _request_error_message(err: Optional[httpx.RequestError]) -> str:
    """Build a self-diagnosing error string carrying the httpx subtype.

    底层 httpx 异常类型（ReadTimeout / ConnectError / …）是区分根因的关键，
    但只落在 WARNING 日志、不进 P2 报警。把类型名带进 RuntimeError，使上层
    ERROR 报警自带「读超时 / 连不上 / 连接重置」语义，无需登机翻日志。
    类型名与 httpx 文案不含密钥，脱敏安全。
    """
    if err is None:
        return "LLM request failed"
    detail = str(err).strip()
    if detail:
        return f"LLM request failed: {type(err).__name__}: {detail}"
    return f"LLM request failed: {type(err).__name__}"


def _post_json(
    *,
    provider: LLMProviderConfig,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    max_attempts = max(1, int(provider.max_retries) + 1)
    last_request_error: Optional[httpx.RequestError] = None
    body_bytes = _json_body_bytes(body)
    for attempt in range(1, max_attempts + 1):
        try:
            _write_request_dump(
                provider=provider,
                url=url,
                body_bytes=body_bytes,
                attempt=attempt,
            )
            with httpx.Client(
                timeout=_chat_timeout(provider),
                trust_env=False,
                transport=_chat_transport(provider),
            ) as client:
                response = client.post(url, headers=headers, content=body_bytes)
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
            # ReadTimeout 来自服务端生成耗时过长（重上下文/慢模型），用同样的 timeout
            # 重试同一重请求几乎无益，只会让用户再多等一个 timeout 窗口 → 快速失败，
            # 不消耗剩余 attempt。连接类瞬时错误（ConnectError/ConnectTimeout 等）仍重试。
            if isinstance(err, httpx.ReadTimeout) or attempt >= max_attempts:
                raise RuntimeError(_request_error_message(err)) from err
            time.sleep(min(0.2 * attempt, 1.0))
    raise RuntimeError(_request_error_message(last_request_error)) from last_request_error


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


def _iter_sse_records(chunks: Iterable[bytes]) -> Iterator[Tuple[str, str]]:
    """Decode arbitrarily split UTF-8 chunks into SSE event/data records."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""

    def pop_records(*, final: bool = False) -> Iterator[Tuple[str, str]]:
        nonlocal buffer
        while True:
            match = re.search(r"\r?\n\r?\n", buffer)
            if match is None:
                break
            block = buffer[: match.start()]
            buffer = buffer[match.end() :]
            record = _parse_sse_block(block)
            if record is not None:
                yield record
        if final and buffer:
            record = _parse_sse_block(buffer)
            buffer = ""
            if record is not None:
                yield record

    for chunk in chunks:
        if not chunk:
            continue
        buffer += decoder.decode(chunk)
        yield from pop_records()
    buffer += decoder.decode(b"", final=True)
    yield from pop_records(final=True)


def _parse_sse_block(block: str) -> Optional[Tuple[str, str]]:
    event_name = "message"
    data_lines: List[str] = []
    for line in block.splitlines():
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
    if not data_lines:
        return None
    return event_name, "\n".join(data_lines)


def _stream_sse_json(
    *,
    provider: LLMProviderConfig,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Iterator[Tuple[str, Optional[Dict[str, Any]]]]:
    """POST an SSE request and yield decoded JSON records; ``None`` is the DONE marker."""
    body_bytes = _json_body_bytes(body)
    max_attempts = max(1, int(provider.max_retries) + 1)
    for attempt in range(1, max_attempts + 1):
        emitted = False
        try:
            _write_request_dump(
                provider=provider,
                url=url,
                body_bytes=body_bytes,
                attempt=attempt,
            )
            with httpx.Client(
                timeout=_chat_timeout(provider),
                trust_env=False,
                transport=_chat_transport(provider),
            ) as client:
                with client.stream("POST", url, headers=headers, content=body_bytes) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        raise RuntimeError("LLM streaming response is not SSE")
                    for event_name, raw_data in _iter_sse_records(response.iter_bytes()):
                        emitted = True
                        if raw_data.strip() == "[DONE]":
                            yield event_name, None
                            continue
                        try:
                            payload = json.loads(raw_data)
                        except json.JSONDecodeError as err:
                            raise RuntimeError("LLM returned invalid SSE JSON") from err
                        if not isinstance(payload, dict):
                            raise RuntimeError("LLM returned invalid SSE payload")
                        yield event_name, payload
            return
        except httpx.HTTPStatusError as err:
            logger.error(
                "llm stream http error provider=%s protocol=%s status=%s",
                provider.id,
                provider.protocol,
                err.response.status_code,
            )
            raise RuntimeError(f"LLM HTTP error: {err.response.status_code}") from err
        except httpx.RequestError as err:
            logger.warning(
                "llm stream request failed provider=%s attempt=%s/%s emitted=%s error=%s",
                provider.id,
                attempt,
                max_attempts,
                emitted,
                type(err).__name__,
            )
            if emitted or isinstance(err, httpx.ReadTimeout) or attempt >= max_attempts:
                raise RuntimeError(_request_error_message(err)) from err
            time.sleep(min(0.2 * attempt, 1.0))


def _normalized_usage(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prompt_tokens": payload.get("prompt_tokens", payload.get("input_tokens")),
        "completion_tokens": payload.get("completion_tokens", payload.get("output_tokens")),
    }


def _openai_chat_stream_events(
    records: Iterable[Tuple[str, Optional[Dict[str, Any]]]],
) -> Iterator[LLMStreamEvent]:
    finish_reason: Optional[str] = None
    response_id: Optional[str] = None
    completed = False
    for _event_name, payload in records:
        if payload is None:
            completed = True
            yield LLMStreamEvent(
                kind="completed",
                finish_reason=finish_reason or "stop",
                response_id=response_id,
            )
            return
        response_id = str(payload.get("id") or response_id or "") or None
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            yield LLMStreamEvent(kind="usage", usage=_normalized_usage(usage))
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                yield LLMStreamEvent(kind="text_delta", text=content, response_id=response_id)
            for tool_delta in delta.get("tool_calls") or []:
                if not isinstance(tool_delta, dict):
                    continue
                function = tool_delta.get("function") or {}
                yield LLMStreamEvent(
                    kind="tool_delta",
                    tool_call={
                        "index": tool_delta.get("index", 0),
                        "id": tool_delta.get("id") or "",
                        "name_delta": function.get("name") or "",
                        "arguments_delta": function.get("arguments") or "",
                    },
                    response_id=response_id,
                )
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    if not completed:
        if finish_reason:
            # A few OpenAI-compatible providers close cleanly after the finish chunk
            # without a separate [DONE] sentinel.
            yield LLMStreamEvent(
                kind="completed",
                finish_reason=finish_reason,
                response_id=response_id,
            )
        else:
            yield LLMStreamEvent(
                kind="error",
                error_code="stream_truncated",
                response_id=response_id,
            )


def _openai_responses_stream_events(
    records: Iterable[Tuple[str, Optional[Dict[str, Any]]]],
) -> Iterator[LLMStreamEvent]:
    response_id: Optional[str] = None
    completed = False
    has_tool_call = False
    for event_name, payload in records:
        if payload is None:
            continue
        event_type = str(payload.get("type") or event_name)
        response = payload.get("response") or {}
        response_id = str(
            payload.get("response_id") or response.get("id") or response_id or ""
        ) or None
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                yield LLMStreamEvent(kind="text_delta", text=delta, response_id=response_id)
        elif event_type == "response.output_item.added":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                has_tool_call = True
                yield LLMStreamEvent(
                    kind="tool_delta",
                    tool_call={
                        "index": payload.get("output_index", 0),
                        "id": item.get("call_id") or item.get("id") or "",
                        "name_delta": item.get("name") or "",
                        "arguments_delta": item.get("arguments") or "",
                    },
                    response_id=response_id,
                )
        elif event_type == "response.function_call_arguments.delta":
            has_tool_call = True
            yield LLMStreamEvent(
                kind="tool_delta",
                tool_call={
                    "index": payload.get("output_index", 0),
                    "id": payload.get("item_id") or "",
                    "name_delta": "",
                    "arguments_delta": payload.get("delta") or "",
                },
                response_id=response_id,
            )
        elif event_type == "response.completed":
            usage = response.get("usage")
            if isinstance(usage, dict) and usage:
                yield LLMStreamEvent(kind="usage", usage=_normalized_usage(usage))
            completed = True
            yield LLMStreamEvent(
                kind="completed",
                finish_reason="tool_calls" if has_tool_call else "stop",
                response_id=response_id,
            )
        elif event_type in {"error", "response.failed", "response.incomplete"}:
            completed = True
            yield LLMStreamEvent(kind="error", error_code=event_type, response_id=response_id)
    if not completed:
        yield LLMStreamEvent(kind="error", error_code="stream_truncated", response_id=response_id)


def _anthropic_stream_events(
    records: Iterable[Tuple[str, Optional[Dict[str, Any]]]],
) -> Iterator[LLMStreamEvent]:
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None
    completed = False
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    for event_name, payload in records:
        if payload is None:
            continue
        event_type = str(payload.get("type") or event_name)
        if event_type == "message_start":
            message = payload.get("message") or {}
            response_id = str(message.get("id") or "") or response_id
            usage = message.get("usage") or {}
            input_tokens = usage.get("input_tokens", input_tokens)
        elif event_type == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                initial_input = block.get("input") or {}
                arguments = json.dumps(initial_input, ensure_ascii=False) if initial_input else ""
                yield LLMStreamEvent(
                    kind="tool_delta",
                    tool_call={
                        "index": payload.get("index", 0),
                        "id": block.get("id") or "",
                        "name_delta": block.get("name") or "",
                        "arguments_delta": arguments,
                    },
                    response_id=response_id,
                )
        elif event_type == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    yield LLMStreamEvent(kind="text_delta", text=text, response_id=response_id)
            elif delta.get("type") == "input_json_delta":
                yield LLMStreamEvent(
                    kind="tool_delta",
                    tool_call={
                        "index": payload.get("index", 0),
                        "id": "",
                        "name_delta": "",
                        "arguments_delta": delta.get("partial_json") or "",
                    },
                    response_id=response_id,
                )
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            if delta.get("stop_reason"):
                finish_reason = "tool_calls" if delta["stop_reason"] == "tool_use" else str(delta["stop_reason"])
            usage = payload.get("usage") or {}
            output_tokens = usage.get("output_tokens", output_tokens)
        elif event_type == "message_stop":
            if input_tokens is not None or output_tokens is not None:
                yield LLMStreamEvent(
                    kind="usage",
                    usage={"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
                )
            completed = True
            yield LLMStreamEvent(
                kind="completed",
                finish_reason=finish_reason or "stop",
                response_id=response_id,
            )
        elif event_type == "error":
            completed = True
            error = payload.get("error") or {}
            yield LLMStreamEvent(
                kind="error",
                error_code=str(error.get("type") or "provider_error"),
                response_id=response_id,
            )
    if not completed:
        yield LLMStreamEvent(kind="error", error_code="stream_truncated", response_id=response_id)


def chat_completion_stream(
    provider: LLMProviderConfig,
    messages: List[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Any = "auto",
) -> Iterator[LLMStreamEvent]:
    """Call a provider in streaming mode and emit provider-neutral events."""
    if provider.protocol == "openai_chat":
        body = _openai_chat_payload(provider, messages, tools=tools, tool_choice=tool_choice)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        records = _stream_sse_json(
            provider=provider,
            url=_openai_chat_url(provider),
            headers=_openai_headers(provider),
            body=body,
        )
        yield from _openai_chat_stream_events(records)
        return
    if provider.protocol == "openai_responses":
        body = _openai_responses_body(provider, messages, tools=tools, tool_choice=tool_choice)
        body["stream"] = True
        records = _stream_sse_json(
            provider=provider,
            url=_openai_responses_url(provider),
            headers=_openai_headers(provider),
            body=body,
        )
        yield from _openai_responses_stream_events(records)
        return
    if provider.protocol == "anthropic_messages":
        body = _anthropic_body(provider, messages, tools=tools, tool_choice=tool_choice)
        body["stream"] = True
        records = _stream_sse_json(
            provider=provider,
            url=_anthropic_messages_url(provider),
            headers=_anthropic_headers(provider),
            body=body,
        )
        yield from _anthropic_stream_events(records)
        return
    raise RuntimeError(f"unsupported LLM protocol: {provider.protocol}")


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
