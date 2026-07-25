"""调试 / Prompt-Lab / web-search 调试路由（/debug/*）。

从 app.main 拆出（结构优化，函数体逐字保留）。settings 在本模块绑定，
测试需 patch "app.routers.debug.settings" 及 generate_completion 等本模块名。
"""
import uuid
import time
import re
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.config import settings
from app.bootstrap.runtime import get_background_loop
from app.routers.deps import get_admin_user, verify_admin_auth
from app.routers.serializers import _audit_plaintext_access, _can_bypass_redaction_for_account, _debug_redaction_payload, _message_for_view, _normalize_ts, _profile_for_view, _prompt_lab_messages_for_view, _prompt_lab_session_for_account, _redact_raw_payload, _redacted_flag_for_account, _require_plaintext_access, _session_for_view, _trace_for_view, _validate_prompt_lab_messages
from app.routers.models import ProfileUpdateRequest
from app.db import ACCOUNT_ACTIVE_SESSION_KEY, cancel_reminder, clear_all_messages_for_account, clear_session_messages, create_search_provider_run, create_tool_invocation, get_account, get_account_onboarding_state, get_debug_trace, get_message_raw, get_or_create_session, get_profile_for_account, get_profile_for_session, get_reminder, get_session, get_tool_invocation, insert_debug_trace, list_debug_traces, list_recent_message_raw, list_reminders_for_account, list_search_provider_runs, list_session_messages, list_sessions, list_sessions_for_account, list_tool_invocations, set_account_debug_flag, set_account_onboarding_state, update_profile_for_session, update_reminder, update_tool_invocation
from app.agent_runtime.llm.service import generate_completion, get_active_llm_model, resolve_active_llm_provider
from app.agent_runtime.llm.providers import get_llm_provider
from app.onboarding import is_onboarding_active
from app.schemas import OpenClawTurnRequest
from app.time_utils import beijing_now
from app.tools import get_web_search_tools
from app.tools.web_search_handlers import handle_web_search, override_provider_order
from app.turn_service import build_turn_llm_input, handle_openclaw_turn
from app.user_profiles import CONTEXT_FILE_ORDER, context_file_exists, context_file_path, ensure_user_profile, read_agent_context, read_context_file
from datetime import date as date_cls, datetime
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


_WEB_SEARCH_DEBUG_PROVIDERS = {"aliyun", "baidu", "bing", "duckduckgo"}


class PromptLabBuildRequest(BaseModel):
    user_text: Optional[str] = Field(default="", max_length=8000)
    session_id: Optional[int] = None
    include_tool_instructions: bool = True
    source_trace_id: Optional[str] = None


class PromptLabReplayRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    session_id: Optional[int] = None
    source_trace_id: Optional[str] = None
    provider_id: Optional[str] = Field(default=None, max_length=100)
    reason: Optional[str] = Field(default=None, max_length=500)


def _preview_session_for_account(account_id: str) -> dict:
    """Return an existing account session for preview, or a side-effect-free stub."""
    sessions = list_sessions_for_account(account_id=account_id, limit=20)
    active = [
        session for session in sessions
        if session.get("session_key") == ACCOUNT_ACTIVE_SESSION_KEY and session.get("status") == "active"
    ]
    if active:
        return active[0]
    if sessions:
        return sessions[0]
    return {
        "id": 0,
        "account_id": account_id,
        "session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "status": "preview",
        "turn_count": 0,
        "business_day": None,
        "carryover_summary": None,
    }


def _build_prompt_lab_envelope(
    *,
    account_id: str,
    session_id: Optional[int],
    user_text: str,
    include_tool_instructions: bool,
    debug_dry_run: bool,
    allow_missing_session: bool = False,
) -> dict:
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    session = (
        _preview_session_for_account(account_id)
        if allow_missing_session
        else _prompt_lab_session_for_account(account_id=account_id, session_id=session_id)
    )
    profile = get_profile_for_account(account_id=account_id) or {}
    current = beijing_now()
    today = current.date().isoformat()
    onboarding_state = get_account_onboarding_state(account_id=account_id)
    llm_input = build_turn_llm_input(
        account_id=account_id,
        account=account,
        session=session,
        profile=profile,
        text=user_text or "",
        today=today,
        current_time=current.strftime("%H:%M"),
        onboarding_state=onboarding_state,
        onboarding_active=is_onboarding_active(onboarding_state),
        web_search_enabled=bool(getattr(settings, "web_search_enabled", False)),
        include_tool_instructions=include_tool_instructions,
        debug_dry_run=debug_dry_run,
    )
    return {
        "account": account,
        "session": session,
        "today": today,
        "llm_input": llm_input,
    }


def _trace_messages_for_prompt_lab(trace: dict) -> list[dict[str, Any]]:
    """Return trace messages, falling back to a system-only prompt for legacy traces."""
    messages = trace.get("messages") or []
    if messages:
        return messages
    system_prompt = str(trace.get("system_prompt") or "")
    if system_prompt:
        return [{"role": "system", "content": system_prompt}]
    return []


def _trace_observability_for_prompt_lab(trace: dict, messages: list[dict[str, Any]]) -> dict:
    """Build display metadata for trace loads that lack full prompt-build observability."""
    metadata = dict(trace.get("metadata") or {})
    system_prompt = str(
        (messages[0].get("content") if messages and isinstance(messages[0], dict) else None)
        or trace.get("system_prompt")
        or ""
    )
    metadata.setdefault("messages_count", len(messages))
    metadata.setdefault("history_count", max(len(messages) - 1, 0))
    metadata.setdefault("system_prompt_chars", len(system_prompt))
    metadata.setdefault("trace_source", trace.get("source"))
    metadata.setdefault("trace_created_at", trace.get("created_at"))

    prompt_blocks = metadata.get("block_metrics") or metadata.get("prompt_blocks") or {}
    if not prompt_blocks and system_prompt:
        prompt_blocks = {
            "trace_system_prompt": {
                "included": True,
                "section": "trace",
                "chars": len(system_prompt),
            }
        }

    tooling = metadata.get("tooling") or {}
    if not tooling:
        tooling = {
            "mode": "trace",
            "available_tool_names": [],
            "available_tools": [],
            "disabled_tools": [],
            "first_round_tool_choice": None,
        }

    history_metadata = metadata.get("history") or {}
    if not history_metadata:
        history_messages = []
        for index, message in enumerate(messages[1:], start=1):
            content = str((message or {}).get("content") or "")
            history_messages.append(
                {
                    "index": index,
                    "role": (message or {}).get("role"),
                    "chars": len(content),
                }
            )
        history_metadata = {
            "count": len(history_messages),
            "session_count": 1 if history_messages else 0,
            "messages": history_messages,
        }

    carryover = metadata.get("carryover") or {"included": False, "suppressed_by_history": False}
    return {
        "metadata": metadata,
        "prompt_blocks": prompt_blocks,
        "tooling": tooling,
        "history_metadata": history_metadata,
        "carryover": carryover,
    }


@router.get("/debug/sessions")
def debug_sessions(limit: int = 50, _: None = Depends(verify_admin_auth)) -> dict:
    return {"sessions": list_sessions(limit=limit)}


@router.get("/debug/messages")
def debug_messages(session_id: int, limit: int = 100, _: None = Depends(verify_admin_auth)) -> dict:
    session = get_session(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    account_id = session.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "session": _session_for_view(session),
        "profile": _profile_for_view(
            get_profile_for_session(session_id=session_id) or {},
            account_id=account_id,
        ),
        "messages": [
            _message_for_view(message, account_id=account_id)
            for message in list_session_messages(session_id=session_id, limit=limit)
        ],
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@router.get("/debug/messages/raw")
def debug_recent_message_raw(limit: int = 20, _: None = Depends(verify_admin_auth)) -> dict:
    messages = list_recent_message_raw(limit=limit)
    return {
        "messages": [
            _message_for_view(message)
            for message in messages
        ],
        "redacted": not any(_can_bypass_redaction_for_account(message.get("account_id")) for message in messages),
    }


@router.get("/debug/messages/{message_db_id}/raw")
def debug_message_raw(message_db_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    message = get_message_raw(message_db_id=message_db_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    account_id = message.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "message": _message_for_view(message),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@router.get("/debug/traces")
def debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "traces": [
            _trace_for_view(t) for t in list_debug_traces(
                account_id=account_id,
                session_id=session_id,
                limit=limit,
            )
        ]
    }


@router.get("/debug/traces/{trace_id}")
def debug_trace(trace_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    trace = get_debug_trace(trace_id=trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    account_id = trace.get("account_id")
    redacted = _redacted_flag_for_account(account_id)
    return {
        "trace": _trace_for_view(trace),
        **(_debug_redaction_payload(account_id=account_id) if not redacted else {"redacted": True}),
    }


@router.get("/debug/accounts/{account_id}/prompt-preview")
def debug_prompt_preview(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Compatibility wrapper over Prompt Lab's unified LLM input builder."""
    built = _build_prompt_lab_envelope(
        account_id=account_id,
        session_id=None,
        user_text="",
        include_tool_instructions=True,
        debug_dry_run=False,
        allow_missing_session=True,
    )
    today = built["today"]
    llm_input = built["llm_input"]
    metadata = llm_input["metadata"]
    prompt = llm_input["system_prompt"]
    blocks = {
        "soul_chars": metadata.get("soul_chars", 0),
        "user_prefs_chars": metadata.get("user_prefs_chars", 0),
        "long_term_memory_chars": metadata.get("long_term_memory_chars", 0),
        "daily_notes_loaded": metadata.get("daily_notes_loaded", False),
        "daily_notes_chars": metadata.get("daily_notes_chars", 0),
        "block_metrics": llm_input.get("prompt_blocks") or {},
        "system_prompt_override": metadata.get("system_prompt_override", False),
        "style": metadata.get("style"),
        "display_name": metadata.get("display_name"),
        "agent_context": metadata.get("agent_context") or {},
        "tooling": {
            key: value
            for key, value in (llm_input.get("tooling") or {}).items()
            if key != "tools"
        },
        "history": llm_input.get("history_metadata") or {},
        "carryover": llm_input.get("carryover") or {},
    }
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "today": today,
            "total_chars": len(prompt),
            "blocks": blocks,
            "metadata": metadata,
            "prompt_blocks": llm_input.get("prompt_blocks") or {},
            "tooling": blocks["tooling"],
            "history_metadata": blocks["history"],
            "carryover": blocks["carryover"],
            "prompt": prompt,
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "today": today,
        "total_chars": len(prompt),
        "blocks": blocks,
        "metadata": metadata,
        "prompt_blocks": llm_input.get("prompt_blocks") or {},
        "tooling": blocks["tooling"],
        "history_metadata": blocks["history"],
        "carryover": blocks["carryover"],
        "prompt_redacted": True,
        "prompt_chars": len(prompt),
        "redacted": True,
    }


@router.get("/debug/accounts/{account_id}/user-profile")
def debug_get_user_profile(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    path = ensure_user_profile(account_id)  # 逻辑路径，仅展示
    context = read_agent_context(account_id)
    content = read_context_file(account_id, "user_profile.md") or ""
    if _can_bypass_redaction_for_account(account_id):
        return {
            "account_id": account_id,
            "path": str(path),
            "content": content,
            "agent_context": context.metadata(),
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "account_id": account_id,
        "path": str(path),
        "content_redacted": True,
        "content_chars": len(content),
        "agent_context": context.metadata(),
        "redacted": True,
    }


@router.get("/debug/prompt-lab/accounts/{account_id}/context-files")
def debug_prompt_lab_context_files(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Return account context files + global skill files for prompt-lab inspection."""
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    context = read_agent_context(account_id)
    plaintext = _can_bypass_redaction_for_account(account_id)
    files = []
    for filename in CONTEXT_FILE_ORDER:
        path = context_file_path(account_id, filename)  # 逻辑路径，仅展示
        raw = read_context_file(account_id, filename)
        exists = raw is not None
        content = raw or ""
        item = {
            "filename": filename,
            "path": str(path),
            "exists": exists,
            "chars": len(content),
            "section": "account",
        }
        if plaintext:
            item["content"] = content
        else:
            item["content_redacted"] = True
        files.append(item)
    # 全局 skill 文件：只读、非账号隔离，不脱敏
    from app.skills import list_skill_catalog, read_skill
    for skill in list_skill_catalog():
        location = skill.get("location", "")
        content = read_skill(location) or ""
        files.append({
            "filename": location,
            "path": location,
            "exists": bool(content),
            "chars": len(content),
            "content": content,
            "section": "skill",
        })
    return {
        "account_id": account_id,
        "agent_context": context.metadata(),
        "files": files,
        **(_debug_redaction_payload(account_id=account_id) if plaintext else {"redacted": True}),
    }


@router.get("/debug/prompt-lab/accounts/{account_id}/conversation")
def debug_prompt_lab_conversation(
    account_id: str,
    session_id: Optional[int] = None,
    limit: int = 80,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Return the selected account conversation and recent prompt traces."""
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    session = _prompt_lab_session_for_account(account_id=account_id, session_id=session_id)
    messages = list_session_messages(session_id=int(session["id"]), limit=limit)
    traces = list_debug_traces(account_id=account_id, session_id=int(session["id"]), limit=20)
    return {
        "account_id": account_id,
        "session": _session_for_view(session),
        "messages": [_message_for_view(message, account_id=account_id) for message in messages],
        "traces": [_trace_for_view(trace) for trace in traces],
        **(_debug_redaction_payload(account_id=account_id) if _can_bypass_redaction_for_account(account_id) else {"redacted": True}),
    }


@router.post("/debug/prompt-lab/accounts/{account_id}/build")
def debug_prompt_lab_build(
    account_id: str,
    payload: PromptLabBuildRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Build the full LLM message list for a dry-run prompt-lab turn."""
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    if payload.source_trace_id:
        trace = get_debug_trace(trace_id=payload.source_trace_id)
        if trace is None or trace.get("account_id") != account_id:
            raise HTTPException(status_code=404, detail="trace not found")
        messages = _trace_messages_for_prompt_lab(trace)
        observability = _trace_observability_for_prompt_lab(trace, messages)
        metadata = observability["metadata"]
        if not _can_bypass_redaction_for_account(account_id):
            metadata = _redact_raw_payload(metadata)
        return {
            "account_id": account_id,
            "source": "trace",
            "source_trace_id": payload.source_trace_id,
            "llm_model": trace.get("llm_model"),
            "metadata": metadata,
            "prompt_blocks": observability["prompt_blocks"],
            "tooling": observability["tooling"],
            "history_metadata": observability["history_metadata"],
            "carryover": observability["carryover"],
            **_prompt_lab_messages_for_view(account_id=account_id, messages=messages),
        }

    built = _build_prompt_lab_envelope(
        account_id=account_id,
        session_id=payload.session_id,
        user_text=payload.user_text or "",
        include_tool_instructions=payload.include_tool_instructions,
        debug_dry_run=True,
        allow_missing_session=payload.session_id is None,
    )
    session = built["session"]
    today = built["today"]
    llm_input = built["llm_input"]
    messages = llm_input["messages"]
    tooling = {
        key: value
        for key, value in (llm_input.get("tooling") or {}).items()
        if key != "tools"
    }
    return {
        "account_id": account_id,
        "source": "build",
        "session": _session_for_view(session),
        "today": today,
        "llm_model": get_active_llm_model(),
        "metadata": llm_input["metadata"],
        "prompt_blocks": llm_input.get("prompt_blocks") or {},
        "tooling": tooling,
        "history_metadata": llm_input.get("history_metadata") or {},
        "carryover": llm_input.get("carryover") or {},
        **_prompt_lab_messages_for_view(account_id=account_id, messages=messages),
    }


@router.post("/debug/prompt-lab/accounts/{account_id}/replay")
def debug_prompt_lab_replay(
    account_id: str,
    payload: PromptLabReplayRequest,
    admin_user: dict = Depends(get_admin_user),
) -> dict:
    """Replay edited prompt-lab messages without writing normal chat state."""
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    grant = None
    if not _can_bypass_redaction_for_account(account_id):
        grant = _require_plaintext_access(
            admin_user=admin_user,
            account_id=account_id,
            resource_type="debug_trace",
        )
    session = _prompt_lab_session_for_account(account_id=account_id, session_id=payload.session_id)
    messages = _validate_prompt_lab_messages(payload.messages)
    selected_provider_id = (payload.provider_id or "").strip()
    try:
        llm_provider = (
            get_llm_provider(selected_provider_id, settings_obj=settings)
            if selected_provider_id
            else resolve_active_llm_provider()
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if selected_provider_id and not llm_provider.enabled:
        raise HTTPException(status_code=400, detail="provider is disabled")
    if selected_provider_id and not llm_provider.api_key:
        raise HTTPException(status_code=400, detail="provider API key is not configured")
    started = time.monotonic()
    reply = None
    error = None
    try:
        reply = generate_completion(messages, provider=llm_provider)
    except Exception as err:
        logger.exception("prompt lab replay failed account=%s error=%s", account_id, err)
        error = str(err)
    latency_ms = int((time.monotonic() - started) * 1000)
    trace_id = f"prompt-lab-{uuid.uuid4()}"
    insert_debug_trace(
        trace_id=trace_id,
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=None,
        source="prompt_lab",
        llm_model=llm_provider.model,
        system_prompt=messages[0]["content"],
        messages=messages,
        reply=reply,
        metadata={
            "trace_kind": "prompt_lab_replay",
            "source_trace_id": payload.source_trace_id,
            "admin_user_id": admin_user.get("id"),
            "reason": payload.reason,
            "llm_provider_id": llm_provider.id,
            "llm_provider_source": llm_provider.source,
            "side_effects": "llm_only_no_message_no_memory_no_outbound",
        },
        latency_ms=latency_ms,
        error=error,
    )
    _audit_plaintext_access(
        admin_user=admin_user,
        action="prompt_lab_replay",
        resource_type="debug_trace",
        resource_id=trace_id,
        account_id=account_id,
        request_path=f"/debug/prompt-lab/accounts/{account_id}/replay",
        reason=payload.reason or "prompt_lab_replay",
        grant_id=int(grant["id"]) if grant else None,
        metadata={
            "source_trace_id": payload.source_trace_id,
            "llm_provider_id": llm_provider.id,
        },
    )
    return {
        "status": "error" if error else "ok",
        "account_id": account_id,
        "trace_id": trace_id,
        "llm_provider_id": llm_provider.id,
        "llm_model": llm_provider.model,
        "reply": reply,
        "latency_ms": latency_ms,
        "error": error,
        "side_effects": "llm_only_no_message_no_memory_no_outbound",
        "plaintext": True,
    }


@router.post("/debug/sessions/{session_id}/reset")
def debug_reset_session(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    if get_session(session_id=session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    deleted = clear_session_messages(session_id=session_id)
    return {"status": "ok", "session_id": session_id, "deleted": deleted}


@router.get("/debug/sessions/{session_id}/profile")
def debug_get_profile(session_id: int, _: None = Depends(verify_admin_auth)) -> dict:
    profile = get_profile_for_session(session_id=session_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"profile": profile}


@router.get("/debug/accounts/{account_id}/onboarding")
def debug_get_onboarding(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Return onboarding state and collected context file contents for an account."""
    from app.onboarding import build_onboarding_prompt_context, is_onboarding_active
    from app.user_profiles import read_agent_context, context_file_exists, CONTEXT_FILE_ORDER
    import re

    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    state = get_account_onboarding_state(account_id=account_id)
    agent_ctx = read_agent_context(account_id)

    identity_text = agent_ctx.blocks.get("IDENTITY", "")
    user_text = agent_ctx.blocks.get("USER", "")
    soul_text = agent_ctx.blocks.get("SOUL", "")

    ai_name: Optional[str] = None
    m = re.search(r"AI 名字[:：]\s*(.+)", identity_text)
    if m:
        ai_name = m.group(1).strip()

    user_name: Optional[str] = None
    m = re.search(r"用户称呼[:：]\s*(.+)", user_text)
    if m:
        user_name = m.group(1).strip()

    from app.db.campaign import get_campaign_attribution

    campaign_attribution = get_campaign_attribution(account_id=account_id)

    prompt_ctx = build_onboarding_prompt_context(
        state=state,
        user_name=user_name,
        ai_name=ai_name,
        persona=None,
        user_name_ask_count=0,
        persona_ask_count=0,
        has_forced_soul_preset=bool((campaign_attribution or {}).get("soul_preset_key")),
        has_forced_ai_name=bool((campaign_attribution or {}).get("ai_name_preset")),
    )

    return {
        "account_id": account_id,
        "onboarding_state": state,
        "onboarding_active": is_onboarding_active(state),
        "campaign_attribution": campaign_attribution,
        "collected": {
            "user_name": user_name,
            "ai_name": ai_name,
            "soul_chars": len(soul_text),
            "soul_preview": soul_text[:200] if soul_text else None,
        },
        "context_files": {
            filename: {
                "exists": context_file_exists(account_id, filename),
                "chars": agent_ctx.files.get(filename, {}).get("chars", 0),
            }
            for filename in CONTEXT_FILE_ORDER
        },
        "prompt_context_preview": prompt_ctx[:500] if prompt_ctx else None,
    }


class OnboardingStateUpdateRequest(BaseModel):
    state: str


@router.patch("/debug/accounts/{account_id}/onboarding/state")
def debug_set_onboarding_state(
    account_id: str,
    payload: OnboardingStateUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Manually set onboarding state — useful for testing specific steps."""
    from app.onboarding import ONBOARDING_COMPLETE, ONBOARDING_TIMED_OUT
    valid_states = {"pending", "step1_sent", "step2_sent", "step3_sent", "complete", "timed_out"}
    if payload.state not in valid_states:
        raise HTTPException(status_code=400, detail=f"invalid state, must be one of: {sorted(valid_states)}")
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    set_account_onboarding_state(account_id=account_id, state=payload.state)
    return {"status": "ok", "account_id": account_id, "onboarding_state": payload.state}


class DebugCreateAccountRequest(BaseModel):
    account_id: Optional[str] = None
    display_name: Optional[str] = None
    campaign_code: Optional[str] = None


@router.post("/debug/accounts/create")
def debug_create_account(
    payload: DebugCreateAccountRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Create a bare test account directly (no turn, no LLM call, state stays pending).

    campaign_code 可选：带上后与生产注册共用 apply_campaign_code_attribution 写入
    account_campaign_attribution 快照并应用强制 SOUL 人设，模拟"用户扫这个活码进来"之后
    onboarding 的分支行为；不带则和之前一样走默认 onboarding 流程。increment_usage=False：
    调试流量不进活码转化统计。
    """
    from app.user_profiles import ensure_user_profile, ensure_agent_context_files
    account_id = (payload.account_id or "").strip() or f"debug-{int(time.time())}"
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id=f"debug-sender-{account_id}",
        sender_name=payload.display_name or None,
        chat_id=f"debug-sender-{account_id}",
        session_key=f"openclaw-weixin:{account_id}:debug-sender-{account_id}",
        business_day=date_cls.today().isoformat(),
    )
    ensure_user_profile(account_id)
    ensure_agent_context_files(account_id, display_name=payload.display_name or None)
    set_account_debug_flag(account_id=account_id, is_debug=True)

    campaign_attribution = None
    if (payload.campaign_code or "").strip():
        from app.db.campaign import apply_campaign_code_attribution
        campaign_attribution = apply_campaign_code_attribution(
            account_id=account_id,
            campaign_code=payload.campaign_code,
            increment_usage=False,
        )

    return {
        "account_id": account_id,
        "status": "created",
        "onboarding_state": "pending",
        "is_debug": True,
        "campaign_attribution": campaign_attribution,
    }


class OnboardingResetRequest(BaseModel):
    clear_context_files: bool = True


@router.post("/debug/accounts/{account_id}/onboarding/reset")
def debug_reset_onboarding(
    account_id: str,
    payload: OnboardingResetRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    """Reset onboarding to pending. Optionally wipe SOUL.md / IDENTITY.md / USER.md.

    Safe to call multiple times. Useful for re-testing the full onboarding flow
    without needing to re-bind a WeChat account.
    """
    from app.agent_runtime.persistence import profile_storage
    from app.user_profiles import delete_context_file, ensure_agent_context_files

    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    set_account_onboarding_state(account_id=account_id, state="pending")

    # Clear all session messages so LLM starts fresh without prior conversation history
    cleared_messages = clear_all_messages_for_account(account_id=account_id)

    cleared = []
    restored_soul_preset = None
    restored_ai_name = None
    if payload.clear_context_files:
        for filename in ("SOUL.md", "IDENTITY.md", "USER.md"):
            if delete_context_file(account_id, filename):
                cleared.append(filename)
        # Clear daily memory notes (memory/YYYY-MM-DD.md rows)
        memory_files = profile_storage.list_filenames(account_id, prefix="memory/")
        if memory_files:
            for fn in memory_files:
                profile_storage.delete_file(account_id, fn)
            cleared.append("memory/")
        # Re-create defaults
        ensure_agent_context_files(account_id, display_name=account.get("display_name"))
        # 若账号带营销活码强制身份，重建默认文件后按注册快照重新落地 AI 名字 + SOUL 人设——否则
        # 空白模板会与 onboarding "已强制身份、跳过对应问句" 分支逻辑漂移，重跑出来的模拟就是错的。
        # 顺序与 apply_campaign_code_attribution 一致：先写 IDENTITY 名字，再渲染 SOUL（§4.4）。
        from app.db.campaign import get_campaign_attribution
        _reset_attribution = get_campaign_attribution(account_id=account_id) or {}
        restored_ai_name = _reset_attribution.get("ai_name_preset")
        restored_soul_preset = _reset_attribution.get("soul_preset_key")
        if restored_ai_name:
            from app.user_profiles import write_ai_name_to_identity
            write_ai_name_to_identity(account_id=account_id, name=restored_ai_name)
        if restored_soul_preset:
            from app.user_profiles import apply_soul_preset
            apply_soul_preset(account_id=account_id, preset_name=restored_soul_preset)

    return {
        "status": "ok",
        "account_id": account_id,
        "onboarding_state": "pending",
        "cleared_files": cleared,
        "cleared_messages": cleared_messages,
        "restored_soul_preset": restored_soul_preset,
        "restored_ai_name": restored_ai_name,
    }


@router.post("/debug/accounts/{account_id}/mark-debug")
def debug_mark_account_as_debug(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    """Mark an existing account as a debug account so its prompt is never redacted."""
    account = get_account(account_id=account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    set_account_debug_flag(account_id=account_id, is_debug=True)
    return {"status": "ok", "account_id": account_id, "is_debug": True}


@router.post("/debug/sessions/{session_id}/profile")
def debug_update_profile(session_id: int, payload: ProfileUpdateRequest, _: None = Depends(verify_admin_auth)) -> dict:
    profile = update_profile_for_session(
        session_id=session_id,
        display_name=payload.display_name,
        style=payload.style,
        system_prompt=payload.system_prompt,
        preferences=payload.preferences,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"status": "ok", "profile": profile}


class ReminderDebugUpdateRequest(BaseModel):
    text: Optional[str] = None
    due_at: Optional[str] = None
    recur_rule: Optional[str] = None
    clear_recur_rule: bool = False


@router.get("/debug/reminders/{account_id}")
def debug_get_reminders(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminders = [
        _normalize_ts(r, "due_at")
        for r in list_reminders_for_account(account_id=account_id, limit=100)
    ]
    return {"account_id": account_id, "reminders": reminders}


@router.patch("/debug/reminders/{reminder_id}")
def debug_patch_reminder(
    reminder_id: str,
    payload: ReminderDebugUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be edited")
    updated = update_reminder(
        reminder_id=reminder_id,
        text=payload.text,
        due_at=payload.due_at,
        recur_rule=payload.recur_rule,
        clear_recur_rule=payload.clear_recur_rule,
    )
    return {"status": "ok", "reminder": updated}


@router.delete("/debug/reminders/{reminder_id}")
def debug_delete_reminder(reminder_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be cancelled")
    cancelled = cancel_reminder(reminder_id=reminder_id)
    return {"status": "ok", "reminder": cancelled}


@router.get("/debug/reminders/{account_id}/content-runs")
def debug_get_reminder_content_runs(
    account_id: str, _: None = Depends(verify_admin_auth)
) -> dict:
    """动态提醒（例行简报）的履约 run 历史，供排查『某期发了什么/为何没发』。"""
    from app.db import list_reminder_content_runs_for_account

    runs = [
        _normalize_ts(r, "scheduled_for")
        for r in list_reminder_content_runs_for_account(account_id=account_id, limit=100)
    ]
    return {"account_id": account_id, "content_runs": runs}


@router.post("/debug/reminders/dynamic/run-once")
def debug_run_dynamic_reminders_once(_: None = Depends(verify_admin_auth)) -> dict:
    """手动跑一轮动态提醒到期履约 + 对账（调试用；受总开关 dynamic_reminder_enabled 约束）。"""
    from app.proactive.obligations.reminders import (
        dispatch_due_dynamic_reminders,
        reconcile_enqueued_reminder_content_runs,
    )

    dispatched = dispatch_due_dynamic_reminders(limit=20)
    reconciled = reconcile_enqueued_reminder_content_runs(limit=100)
    return {"status": "ok", "dispatched": dispatched, "reconciled": reconciled}


class WebSearchDebugSimulationRequest(BaseModel):
    query: str
    provider: Optional[str] = None
    status: str = "succeeded"
    count: int = Field(default=3, ge=1, le=10)
    latency_ms: Optional[int] = Field(default=None, ge=0)
    error: Optional[str] = None


class WebSearchDebugChatRequest(BaseModel):
    text: str
    force_web_search_enabled: bool = True
    provider: Optional[str] = None


class WebSearchDebugRunRequest(BaseModel):
    query: str
    provider: Optional[str] = None
    count: int = Field(default=3, ge=1, le=10)
    language: Optional[str] = None
    country: Optional[str] = None
    freshness: Optional[str] = None
    date_after: Optional[str] = None
    date_before: Optional[str] = None


def _web_search_debug_capabilities() -> dict:
    default_provider = getattr(settings, "web_search_default_provider", "duckduckgo")
    provider_order = [
        item.strip()
        for item in str(getattr(settings, "web_search_provider_order", "") or default_provider).split(",")
        if item.strip()
    ]
    configured_providers = {
        "aliyun": bool(
            getattr(settings, "aliyun_web_search_enabled", False)
            and (
                getattr(settings, "aliyun_web_search_api_key", "")
                or getattr(settings, "dashscope_api_key", "")
            )
        ),
        "baidu": bool(getattr(settings, "baidu_ai_search_enabled", False) and getattr(settings, "baidu_ai_search_api_key", "")),
        "bing": True,
        "duckduckgo": True,
    }
    return {
        "tool_schema_defined": True,
        "model_exposure_configured": bool(getattr(settings, "web_search_enabled", False)),
        "currently_in_turn_tools": bool(getattr(settings, "web_search_enabled", False)),
        "debug_chat_forces_tool_exposure": True,
        "provider_adapter_ready": any(configured_providers.get(provider, False) for provider in provider_order),
        "default_provider": default_provider,
        "provider_order": provider_order,
        "provider_failover": bool(getattr(settings, "web_search_provider_failover", True)),
        "configured_providers": configured_providers,
        "sync_timeout_seconds": getattr(settings, "web_search_sync_timeout_seconds", 8.0),
        "max_results": getattr(settings, "web_search_max_results", 5),
    }


def _web_search_debug_conversation(account_id: str, *, limit: int = 100) -> dict:
    sessions = list_sessions_for_account(account_id=account_id, limit=20)
    active_session = next(
        (session for session in sessions if session.get("session_key") == ACCOUNT_ACTIVE_SESSION_KEY),
        None,
    )
    messages = []
    if active_session is not None:
        messages = list_session_messages(session_id=int(active_session["id"]), limit=limit)
    return {
        "session": active_session,
        "messages": messages,
    }


def _fake_web_search_results(*, query: str, count: int) -> list[dict]:
    return [
        {
            "title": f"Debug result {idx + 1}: {query}",
            "url": f"https://example.com/search-debug/{idx + 1}",
            "snippet": "This is a synthetic web_search debug result. No external provider was called.",
            "site_name": "example.com",
            "retrieved_at": datetime.now().isoformat(timespec="seconds"),
            "score": round(1.0 - idx * 0.08, 2),
        }
        for idx in range(count)
    ]


def _debug_provider_override(provider: Optional[str]) -> Optional[list[str]]:
    cleaned = str(provider or "").strip().lower()
    if cleaned and cleaned not in _WEB_SEARCH_DEBUG_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unsupported web_search provider: {cleaned}")
    return [cleaned] if cleaned else None


@router.get("/debug/web-search/{account_id}")
def debug_get_web_search(
    account_id: str,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    return {
        "account_id": account_id,
        "capabilities": _web_search_debug_capabilities(),
        "tool_schema": get_web_search_tools()[0],
        "tool_invocations": list_tool_invocations(
            account_id=account_id,
            tool_name="web_search",
            limit=limit,
        ),
        "provider_runs": list_search_provider_runs(
            account_id=account_id,
            limit=limit,
        ),
        "conversation": _web_search_debug_conversation(account_id),
    }


@router.get("/debug/web-search/invocations/{tool_invocation_id}")
def debug_get_web_search_invocation(
    tool_invocation_id: int,
    _: None = Depends(verify_admin_auth),
) -> dict:
    invocation = get_tool_invocation(tool_invocation_id=tool_invocation_id)
    if invocation is None:
        raise HTTPException(status_code=404, detail="tool invocation not found")
    return {
        "tool_invocation": invocation,
        "provider_runs": list_search_provider_runs(
            tool_invocation_id=tool_invocation_id,
            limit=50,
        ),
    }


@router.post("/debug/web-search/{account_id}/chat")
def debug_chat_web_search(
    account_id: str,
    payload: WebSearchDebugChatRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    text = str(payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    turn_payload = OpenClawTurnRequest(
        message_id=f"debug-web-search-chat-{uuid.uuid4().hex[:12]}",
        channel="debug-web-search",
        channel_account_id=account_id,
        account_id=account_id,
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        chat_type="private",
        session_key=account_id,
        message_type="text",
        text=text,
        raw={
            "source": "web_search_debug",
            "web_search_provider_override": str(payload.provider or "").strip() or None,
        },
    )
    with override_provider_order(_debug_provider_override(payload.provider)):
        turn = handle_openclaw_turn(
            turn_payload,
            background_loop=get_background_loop(),
            force_web_search_enabled=bool(payload.force_web_search_enabled),
        )
    return {
        "status": "ok",
        "provider_override": str(payload.provider or "").strip() or None,
        "turn": turn.model_dump(),
        "conversation": _web_search_debug_conversation(account_id),
        "tool_invocations": list_tool_invocations(
            account_id=account_id,
            tool_name="web_search",
            limit=50,
        ),
        "provider_runs": list_search_provider_runs(
            account_id=account_id,
            limit=50,
        ),
    }


@router.post("/debug/web-search/{account_id}/run")
def debug_run_web_search(
    account_id: str,
    payload: WebSearchDebugRunRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    query = str(payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    provider_override = _debug_provider_override(payload.provider)
    provider_override_name = str(payload.provider or "").strip() or None

    session_bundle = get_or_create_session(
        account_id=account_id,
        channel="debug",
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        session_key=f"debug-web-search-{account_id}",
    )
    session = session_bundle["session"]
    tool_call_id = f"call_debug_web_search_run_{uuid.uuid4().hex[:12]}"
    args = {
        "query": query,
        "count": payload.count,
        "language": payload.language,
        "country": payload.country,
        "freshness": payload.freshness,
        "date_after": payload.date_after,
        "date_before": payload.date_before,
    }
    invocation = create_tool_invocation(
        account_id=account_id,
        session_id=int(session["id"]),
        message_id=f"debug-web-search-run-{uuid.uuid4().hex[:12]}",
        tool_call_id=tool_call_id,
        tool_name="web_search",
        args={**args, "provider_override": provider_override_name},
        status="running",
    )
    ctx = SimpleNamespace(
        account_id=account_id,
        session=session,
        message_id=f"debug-web-search-run-{uuid.uuid4().hex[:12]}",
    )
    started = datetime.now()
    with override_provider_order(provider_override):
        result = handle_web_search(
            args,
            ctx,
            tool_call_id=tool_call_id,
            tool_invocation_id=int(invocation["id"]),
        )
    latency_ms = int((datetime.now() - started).total_seconds() * 1000)
    status_value = "failed" if result.get("status") == "failed" or result.get("error") else "succeeded"
    updated_invocation = update_tool_invocation(
        tool_invocation_id=int(invocation["id"]),
        status=status_value,
        result=result,
        latency_ms=latency_ms,
        error=result.get("error") if status_value == "failed" else None,
        finished=True,
    )
    return {
        "status": "ok",
        "account_id": account_id,
        "provider_override": provider_override_name,
        "result": result,
        "tool_invocation": updated_invocation,
        "provider_runs": list_search_provider_runs(
            tool_invocation_id=int(invocation["id"]),
            limit=50,
        ),
    }


@router.post("/debug/web-search/{account_id}/simulate")
def debug_simulate_web_search(
    account_id: str,
    payload: WebSearchDebugSimulationRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    query = str(payload.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    status_value = str(payload.status or "succeeded").strip().lower()
    if status_value not in {"running", "queued", "succeeded", "failed"}:
        raise HTTPException(
            status_code=400,
            detail="status must be one of: running, queued, succeeded, failed",
        )

    session_bundle = get_or_create_session(
        account_id=account_id,
        channel="debug",
        sender_id="web-search-debug",
        sender_name="Web Search Debug",
        chat_id=account_id,
        session_key=f"debug-web-search-{account_id}",
    )
    session_id = int(session_bundle["session"]["id"])
    provider = (
        str(payload.provider or "").strip()
        or getattr(settings, "web_search_default_provider", "duckduckgo")
    )
    args = {"query": query, "count": payload.count}
    finished = status_value in {"queued", "succeeded", "failed"}
    if status_value == "queued":
        result = {
            "status": "queued",
            "task_id": f"debug-task-{uuid.uuid4().hex[:12]}",
            "query": query,
        }
    elif status_value == "failed":
        result = {
            "status": "failed",
            "query": query,
            "error": payload.error or "debug simulated provider failure",
        }
    elif status_value == "running":
        result = {"status": "running", "query": query}
    else:
        result = {
            "status": "succeeded",
            "query": query,
            "provider": provider,
            "results": _fake_web_search_results(query=query, count=payload.count),
        }

    invocation = create_tool_invocation(
        account_id=account_id,
        session_id=session_id,
        message_id=f"debug-web-search-{uuid.uuid4().hex[:12]}",
        tool_call_id=f"call_debug_web_search_{uuid.uuid4().hex[:12]}",
        tool_name="web_search",
        args=args,
        status=status_value,
        result=result,
        latency_ms=payload.latency_ms,
        error=payload.error if status_value == "failed" else None,
        finished=finished,
    )

    provider_run = None
    if status_value != "queued":
        provider_status = "failed" if status_value == "failed" else status_value
        provider_run = create_search_provider_run(
            account_id=account_id,
            tool_invocation_id=int(invocation["id"]),
            provider=provider,
            attempt=1,
            status=provider_status,
            request=args,
            response=result if status_value == "succeeded" else {},
            latency_ms=payload.latency_ms,
            error=payload.error if status_value == "failed" else None,
            finished=status_value in {"succeeded", "failed"},
        )

    return {
        "status": "ok",
        "account_id": account_id,
        "tool_invocation": invocation,
        "provider_run": provider_run,
    }
