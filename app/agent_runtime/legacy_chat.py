"""Compatibility runtime for the legacy ``POST /agent/chat`` contract.

Nooki's original mini-program AI adapter still calls this stateless endpoint.
The newer product turn runtime remains authoritative for ``/api/v1/products/nooki``;
this module only preserves the old skill-based request/response boundary.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent_runtime.llm.service import generate_completion


logger = logging.getLogger("ai4all.agent_runtime.legacy_chat")
_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


class AgentChatRequest(BaseModel):
    """Legacy skill-agent request payload used by Nooki adapters."""

    app: str
    user_id: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)


class AgentChatResponse(BaseModel):
    """Legacy skill-agent response payload."""

    reply: str
    understanding: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _load_skill(name: str) -> tuple[str, dict[str, Any]]:
    """Load one allowlisted skill directory without permitting path traversal."""

    if not name or Path(name).name != name:
        raise ValueError(f"Unknown skill: {name!r}")
    skill_dir = _SKILLS_DIR / name
    prompt_path = skill_dir / "skill.md"
    schema_path = skill_dir / "schema.json"
    if not skill_dir.is_dir() or not prompt_path.is_file() or not schema_path.is_file():
        raise ValueError(f"Unknown skill: {name!r}")
    return (
        prompt_path.read_text(encoding="utf-8"),
        json.loads(schema_path.read_text(encoding="utf-8")),
    )


def _build_messages(request: AgentChatRequest) -> list[dict[str, str]]:
    skill_parts: list[str] = []
    primary_schema: dict[str, Any] | None = None
    for name in request.skills:
        prompt, schema = _load_skill(name)
        skill_parts.append(prompt)
        if primary_schema is None:
            primary_schema = schema

    schema_text = json.dumps(primary_schema or {"type": "object"}, ensure_ascii=False, indent=2)
    business_context = json.dumps(request.context, ensure_ascii=False)
    system = "\n\n".join(
        [
            "你是 AI4ALL Agent。只输出一个合法 JSON 对象，不要输出 Markdown 或额外说明。",
            *skill_parts,
            f"输出必须符合此 schema：\n{schema_text}",
            f"调用应用：{request.app}\n用户 ID：{request.user_id}\n业务上下文：{business_context}",
            "再次强调：只输出 JSON 对象本身。",
        ]
    )
    history = request.context.get("history", [])
    safe_history = [
        {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
        for item in history
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]
    return [{"role": "system", "content": system}, *safe_history, {"role": "user", "content": request.message}]


def _parse_output(raw: str) -> dict[str, Any]:
    """Parse model JSON and return a stable non-throwing fallback."""

    text = (raw or "").strip()
    candidates = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.append(match.group(1).strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, json.JSONDecodeError):
            continue
    return {
        "reply": text,
        "actions": [{"type": "none"}],
        "recommendations": [],
        "parse_error": True,
    }


def run_agent(request: AgentChatRequest) -> AgentChatResponse:
    """Execute one stateless legacy skill-agent turn."""

    started = time.monotonic()
    parsed = _parse_output(generate_completion(_build_messages(request)))
    understanding = {
        key: value
        for key, value in parsed.items()
        if key not in {"reply", "recommendations", "parse_error", "raw"}
    }
    metadata: dict[str, Any] = {"latency_ms": int((time.monotonic() - started) * 1000)}
    if parsed.get("parse_error"):
        metadata["parse_error"] = True
    logger.info(
        "legacy_agent_run app=%s user=%s skills=%s latency_ms=%s parse_error=%s",
        request.app,
        request.user_id,
        request.skills,
        metadata["latency_ms"],
        metadata.get("parse_error", False),
    )
    return AgentChatResponse(
        reply=str(parsed.get("reply") or ""),
        understanding=understanding,
        recommendations=parsed.get("recommendations") or [],
        metadata=metadata,
    )
