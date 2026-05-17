import json
import logging
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from app.config import settings


logger = logging.getLogger("ai4all.llm")


def _fallback_reply(text: str) -> str:
    return f"AI4ALL mock 已收到：{text or '空消息'}"


def _http_chat(messages: List[Dict[str, str]]) -> str:
    """Send a messages list to the LLM and return the reply content."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout_seconds) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        logger.error("llm http error status=%s body=%s", err.code, detail[:500])
        raise RuntimeError(f"LLM HTTP error: {err.code}") from err
    except Exception as err:
        logger.error("llm request failed: %s", err)
        raise RuntimeError("LLM request failed") from err

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
