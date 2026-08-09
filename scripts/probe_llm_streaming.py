"""Probe configured LLM providers for real streaming without printing content or secrets."""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

import httpx

from app.agent_runtime.llm.adapters import (
    _anthropic_body,
    _anthropic_headers,
    _anthropic_messages_url,
    _chat_timeout,
    _chat_transport,
    _json_body_bytes,
    _openai_chat_payload,
    _openai_chat_url,
    _openai_headers,
    _openai_responses_body,
    _openai_responses_url,
)
from app.agent_runtime.llm.providers import LLMProviderConfig, list_llm_providers


_PROBE_MESSAGES = [
    {"role": "system", "content": "Reply concisely."},
    {"role": "user", "content": "请用一句不超过二十个字的中文回复，说明流式探针正常。"},
]


@dataclass
class ProbeResult:
    provider_id: str
    protocol: str
    model: str
    status_code: Optional[int] = None
    content_type: str = ""
    wire_chunks: int = 0
    events: int = 0
    text_events: int = 0
    visible_chars: int = 0
    first_text_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    terminal_event: str = ""
    compatible: bool = False
    error: str = ""

    def safe_dict(self) -> Dict[str, Any]:
        """Return metrics only; never include response text, prompt, URL, or key."""
        return self.__dict__.copy()


def _request_parts(provider: LLMProviderConfig) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    if provider.protocol == "openai_chat":
        body = _openai_chat_payload(provider, _PROBE_MESSAGES)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        return _openai_chat_url(provider), _openai_headers(provider), body
    if provider.protocol == "openai_responses":
        body = _openai_responses_body(provider, _PROBE_MESSAGES)
        body["stream"] = True
        return _openai_responses_url(provider), _openai_headers(provider), body
    if provider.protocol == "anthropic_messages":
        body = _anthropic_body(provider, _PROBE_MESSAGES)
        body["stream"] = True
        return _anthropic_messages_url(provider), _anthropic_headers(provider), body
    raise ValueError(f"unsupported protocol: {provider.protocol}")


def _iter_sse(response: httpx.Response, result: ProbeResult) -> Iterator[Tuple[str, str]]:
    event_name = "message"
    data_lines = []
    for line in response.iter_lines():
        result.wire_chunks += 1
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _visible_delta(protocol: str, event_name: str, payload: Dict[str, Any]) -> str:
    event_type = str(payload.get("type") or event_name)
    if protocol == "openai_chat":
        choices = payload.get("choices") or []
        if not choices:
            return ""
        content = (choices[0].get("delta") or {}).get("content")
        return content if isinstance(content, str) else ""
    if protocol == "openai_responses" and event_type == "response.output_text.delta":
        delta = payload.get("delta")
        return delta if isinstance(delta, str) else ""
    if protocol == "anthropic_messages" and event_type == "content_block_delta":
        delta = payload.get("delta") or {}
        text = delta.get("text") if delta.get("type") == "text_delta" else ""
        return text if isinstance(text, str) else ""
    return ""


def _is_terminal(protocol: str, event_name: str, payload: Dict[str, Any]) -> bool:
    event_type = str(payload.get("type") or event_name)
    if protocol == "openai_chat":
        choices = payload.get("choices") or []
        return bool(choices and choices[0].get("finish_reason"))
    if protocol == "openai_responses":
        return event_type in {"response.completed", "response.failed", "response.incomplete"}
    if protocol == "anthropic_messages":
        return event_type in {"message_stop", "error"}
    return False


def probe(provider: LLMProviderConfig) -> ProbeResult:
    result = ProbeResult(provider.id, provider.protocol, provider.model)
    started = time.monotonic()
    try:
        url, headers, body = _request_parts(provider)
        with httpx.Client(
            timeout=_chat_timeout(provider),
            trust_env=False,
            transport=_chat_transport(provider),
        ) as client:
            with client.stream("POST", url, headers=headers, content=_json_body_bytes(body)) as response:
                result.status_code = response.status_code
                result.content_type = response.headers.get("content-type", "")
                response.raise_for_status()
                for event_name, raw_data in _iter_sse(response, result):
                    result.events += 1
                    if raw_data == "[DONE]":
                        result.terminal_event = "[DONE]"
                        continue
                    try:
                        payload = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue
                    delta = _visible_delta(provider.protocol, event_name, payload)
                    if delta:
                        result.text_events += 1
                        result.visible_chars += len(delta)
                        if result.first_text_ms is None:
                            result.first_text_ms = round((time.monotonic() - started) * 1000)
                    if _is_terminal(provider.protocol, event_name, payload):
                        result.terminal_event = str(payload.get("type") or event_name)
        result.duration_ms = round((time.monotonic() - started) * 1000)
        result.compatible = bool(
            result.status_code == 200
            and "text/event-stream" in result.content_type.lower()
            and result.text_events > 0
            and result.terminal_event
        )
    except (httpx.HTTPError, ValueError) as err:
        result.duration_ms = round((time.monotonic() - started) * 1000)
        result.error = type(err).__name__
    return result


def _selected_providers(provider_ids: Iterable[str]) -> list[LLMProviderConfig]:
    configured = {provider.id: provider for provider in list_llm_providers()}
    ids = list(provider_ids)
    if ids == ["all"]:
        return [provider for provider in configured.values() if provider.enabled]
    missing = [provider_id for provider_id in ids if provider_id not in configured]
    if missing:
        raise SystemExit(f"unknown provider id(s): {', '.join(missing)}")
    return [configured[provider_id] for provider_id in ids]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", default=[], help="Provider id; repeatable")
    args = parser.parse_args()
    provider_ids = args.provider or ["all"]
    results = [probe(provider) for provider in _selected_providers(provider_ids)]
    print(json.dumps([result.safe_dict() for result in results], ensure_ascii=False, indent=2))
    if not all(result.compatible for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
