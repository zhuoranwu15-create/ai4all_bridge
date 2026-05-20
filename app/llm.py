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
