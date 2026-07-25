"""脱敏 / 视图序列化 helper + 明文访问治理 helper。

从 app.main 拆出（结构优化，函数体逐字保留）。仍留在 main 的 admin/debug/web
handler 通过 `from app.routers.serializers import *` 取用这些名字。settings 在本
模块绑定，测试需 patch "app.routers.serializers.settings"。
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException, status

from app.config import settings
from app.time_utils import beijing_now_str
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    connect as db_connect,
    find_active_admin_plaintext_grant,
    get_account,
    get_content_invitation,
    get_session,
    insert_admin_access_event,
    list_sessions_for_account,
)
from app.products.zhaoxi.proactive.store.account_state import format_state_time
from app.products.zhaoxi.proactive.preferences import PROACTIVE_FREQUENCY_BUCKETS, resolve_frequency_limits
from app.products.zhaoxi.proactive.store.candidates import get_reactivation_candidate_from_metadata


def _normalize_optional_state_datetime(value: Optional[str], *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an ISO datetime or YYYY-MM-DD HH:MM:SS",
        )
    return format_state_time(parsed)


_REDACTED_TEXT_FIELDS = {
    "content",
    "text",
    "reply",
    "system_prompt",
    "prompt",
    "messages",
    "raw_payload",
    "message",
    "description",
    "caption",
}
_METADATA_TEXT_FIELDS = {
    "source",
    "channel",
    "channel_account_id",
    "openclaw_session_key",
    "account_active_session_key",
    "sender_id",
    "chat_id",
    "message_id",
    "message_type",
    "reply_to_message_id",
    "trace_kind",
    "mode",
    "identity",
    "ai4all_bridge",
    "raw_keys",
}


def _content_meta(value: Any) -> dict:
    text = "" if value is None else str(value)
    return {
        "redacted": True,
        "chars": len(text),
        "preview": None,
    }


def _redact_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if (
                normalized in _REDACTED_TEXT_FIELDS
                or normalized.endswith(("_content", "_text", "_prompt", "_reply"))
                or normalized.startswith(("content_", "text_", "prompt_", "reply_"))
            ):
                redacted[key_text] = _content_meta(item)
            elif isinstance(item, (dict, list)):
                redacted[key_text] = _redact_raw_payload(item)
            else:
                redacted[key_text] = item
        return redacted
    if isinstance(value, list):
        return [_redact_raw_payload(item) for item in value]
    return value


def _redact_message(message: dict) -> dict:
    item = dict(message)
    content = item.pop("content", None)
    item["content_redacted"] = True
    item["content_chars"] = len(content or "")
    item["content_preview"] = None
    if "raw" in item:
        item["raw_redacted"] = True
        item["raw"] = _redact_raw_payload(item.get("raw") or {})
    return item


def _redact_trace(trace: dict) -> dict:
    item = dict(trace)
    system_prompt = item.pop("system_prompt", None)
    messages = item.pop("messages", None)
    reply = item.pop("reply", None)
    item["system_prompt_redacted"] = True
    item["system_prompt_chars"] = len(system_prompt or "")
    item["messages_redacted"] = True
    item["messages_count"] = len(messages or []) if isinstance(messages, list) else 0
    item["messages_chars"] = sum(
        len(str(message.get("content") or ""))
        for message in messages or []
        if isinstance(message, dict)
    )
    item["reply_redacted"] = True
    item["reply_chars"] = len(reply or "")
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


def _redact_profile(profile: dict) -> dict:
    item = dict(profile or {})
    if "system_prompt" in item:
        system_prompt = item.pop("system_prompt")
        item["system_prompt_redacted"] = True
        item["system_prompt_chars"] = len(system_prompt or "")
    if "preferences_json" in item:
        prefs = item.pop("preferences_json")
        item["preferences_redacted"] = True
        item["preferences_chars"] = len(prefs or "")
    return item


def _redact_session(session: dict) -> dict:
    item = dict(session or {})
    for field in ("session_summary", "carryover_summary"):
        if field in item:
            value = item.pop(field)
            item[f"{field}_redacted"] = True
            item[f"{field}_chars"] = len(value or "")
    return item


def _redact_text_field(item: dict, field: str = "text") -> dict:
    redacted = dict(item or {})
    value = redacted.pop(field, None)
    redacted[f"{field}_redacted"] = True
    redacted[f"{field}_chars"] = len(value or "")
    redacted[f"{field}_preview"] = None
    if "metadata" in redacted:
        redacted["metadata"] = _redact_raw_payload(redacted.get("metadata") or {})
    return redacted


def _proactive_state_for_overview(state: Optional[dict]) -> Optional[dict]:
    if state is None:
        return None
    item = _normalize_ts(state, "next_scan_at", "last_scan_at", "last_proactive_sent_at", "cooldown_until")
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


def _proactive_message_settings_with_resolved(eff: dict) -> dict:
    """在有效设定基础上补充每个频次桶的实际有效值（含全局默认），便于后台展示。"""
    # 全局桶日上限（从分类 registry 派生，与 policy._category_daily_limit 同源）
    from app.products.zhaoxi.proactive.contract.categories import CATEGORY_SPECS

    _global_day = {
        spec.frequency_bucket: int(
            getattr(settings, spec.daily_limit_setting, spec.daily_limit_default)
            or spec.daily_limit_default
        )
        for spec in CATEGORY_SPECS
        if spec.frequency_bucket and spec.daily_limit_setting
    }
    resolved = {}
    for bucket in PROACTIVE_FREQUENCY_BUCKETS:
        limits = resolve_frequency_limits(eff, bucket)
        global_day = _global_day.get(bucket, 0)
        resolved[bucket] = {
            "max_per_day": limits["max_per_day"] if limits["day_is_user"] else global_day,
            "max_per_week": limits["max_per_week"],
            "source": "user" if limits["day_is_user"] or limits["max_per_week"] is not None else "global",
        }
    return {**eff, "frequency_resolved": resolved}


def _content_invitation_for_overview(invitation: dict) -> dict:
    item = _normalize_ts(invitation or {})
    invitation_text = item.pop("invitation_text", None)
    titles = item.pop("title_items", None) or []
    item["invitation_text_redacted"] = True
    item["invitation_text_chars"] = len(invitation_text or "")
    item["title_count"] = len(titles)
    if "metadata" in item:
        item["metadata"] = _redact_raw_payload(item.get("metadata") or {})
    return item


_BEIJING_TZ = timezone(timedelta(hours=8))


def _beijing_display(value: Any) -> Any:
    """Attach Beijing +08:00 offset to a naive datetime string for admin display.

    All DB timestamps are now stored as naive Beijing-local strings. This attaches
    the explicit +08:00 offset so the admin frontend renders them correctly.
    """
    text = str(value or "").strip()
    if not text:
        return value
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_BEIJING_TZ)
    else:
        dt = dt.astimezone(_BEIJING_TZ)
    return dt.isoformat()


_TS_FIELDS = ("created_at", "updated_at", "first_seen_at", "last_seen_at", "started_at")


def _normalize_ts(item: dict, *extra_fields: str) -> dict:
    """Return a copy with standard timestamp fields normalized to Beijing-offset ISO strings."""
    result = dict(item)
    for field in _TS_FIELDS + extra_fields:
        if result.get(field) is not None:
            result[field] = _beijing_display(result[field])
    return result


def _content_invitation_for_reactivation_admin(invitation: Optional[dict]) -> Optional[dict]:
    if invitation is None:
        return None
    titles = invitation.get("title_items") or []
    return {
        "id": invitation.get("id"),
        "status": invitation.get("status"),
        "topic": invitation.get("topic"),
        "title_count": len(titles),
        "scheduled_at": _beijing_display(invitation.get("scheduled_at")),
        "expires_at": _beijing_display(invitation.get("expires_at")),
        "invited_at": _beijing_display(invitation.get("invited_at")),
        "updated_at": _beijing_display(invitation.get("updated_at")),
    }


def _list_reactivation_candidate_admin_items(
    *,
    candidate_type: Optional[str],
    limit: int,
) -> list[dict]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id AS account_id,
                a.display_name,
                a.status AS account_status,
                a.updated_at AS account_updated_at,
                MAX(m.created_at) AS account_last_active_at,
                s.enabled,
                s.next_scan_at,
                s.last_scan_at,
                s.last_proactive_sent_at,
                s.cooldown_until,
                s.metadata_json,
                s.updated_at AS proactive_state_updated_at
            FROM proactive_account_state s
            JOIN accounts a ON a.id = s.account_id
            LEFT JOIN messages m ON m.account_id = a.id
            -- 含 a.id（accounts 主键）：PG 据此放行所选 a.* 列的函数依赖（s.account_id 已覆盖 s.*）
            GROUP BY s.account_id, a.id
            ORDER BY s.updated_at DESC, a.updated_at DESC
            LIMIT ?
            """,
            (max(limit, 1),),
        ).fetchall()

    items: list[dict] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        candidate = get_reactivation_candidate_from_metadata(metadata)
        if candidate is None:
            continue
        if candidate_type and candidate.get("type") != candidate_type:
            continue
        invitation = None
        invitation_id = candidate.get("content_invitation_id")
        if invitation_id:
            invitation = get_content_invitation(invitation_id=str(invitation_id))
        candidate_view = dict(candidate)
        for field in ("generated_at", "scheduled_at"):
            if candidate_view.get(field):
                candidate_view[field] = _beijing_display(candidate_view[field])
        items.append(
            {
                "account": {
                    "id": row["account_id"],
                    "display_name": row["display_name"],
                    "status": row["account_status"],
                    "updated_at": _beijing_display(row["account_updated_at"]),
                    "last_active_at": _beijing_display(row["account_last_active_at"]),
                },
                "proactive_state": {
                    "enabled": bool(row["enabled"]),
                    "next_scan_at": _beijing_display(row["next_scan_at"]),
                    "last_scan_at": _beijing_display(row["last_scan_at"]),
                    "last_proactive_sent_at": _beijing_display(row["last_proactive_sent_at"]),
                    "cooldown_until": _beijing_display(row["cooldown_until"]),
                    "updated_at": _beijing_display(row["proactive_state_updated_at"]),
                },
                "reactivation_candidate": candidate_view,
                "content_invitation": _content_invitation_for_reactivation_admin(invitation),
            }
        )
    return items


def _redact_phone(phone: Optional[str]) -> Optional[str]:
    text = str(phone or "").strip()
    if not text:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * max(0, len(text) - 4) + text[-4:]


def _redact_platform_user(user: Optional[dict]) -> Optional[dict]:
    if user is None:
        return None
    item = dict(user)
    item["phone_redacted"] = True
    item["phone_last4"] = str(item.get("phone") or "")[-4:] if item.get("phone") else None
    item["phone"] = _redact_phone(item.get("phone"))
    return item


def _binding_intent_for_view(intent: dict, *, account_id: Optional[str]) -> dict:
    item = dict(intent or {})
    if _can_bypass_redaction_for_account(account_id):
        item["plaintext_debug"] = True
        return item
    qr_data_url = item.pop("qr_data_url", None)
    manual_login_command = item.pop("manual_login_command", None)
    raw_result = item.pop("raw_result", None)
    item["qr_data_url_redacted"] = bool(qr_data_url)
    item["manual_login_command_redacted"] = bool(manual_login_command)
    item["raw_result_redacted"] = bool(raw_result)
    if isinstance(raw_result, dict):
        item["raw_result_keys"] = sorted(str(key) for key in raw_result.keys())
    return item


def _debug_plaintext_account_allowlist() -> set[str]:
    raw = getattr(settings, "admin_debug_plaintext_account_allowlist", "") or ""
    return {value.strip() for value in raw.split(",") if value.strip()}


def _is_non_production_env() -> bool:
    return str(getattr(settings, "app_env", "") or "").lower() in {"local", "development", "test"}


def _can_bypass_redaction_for_account(account_id: Optional[str]) -> bool:
    if not account_id:
        return False
    if account_id in _debug_plaintext_account_allowlist():
        return True
    account = get_account(account_id=account_id)
    if account and account.get("is_debug"):
        return True
    if bool(getattr(settings, "admin_debug_plaintext_enabled", False)):
        return _is_non_production_env()
    return False


def _message_plaintext(message: dict) -> dict:
    item = dict(message)
    item["plaintext_debug"] = True
    return item


def _trace_plaintext(trace: dict) -> dict:
    item = dict(trace)
    item["plaintext_debug"] = True
    return item


def _profile_plaintext(profile: dict) -> dict:
    item = dict(profile or {})
    item["plaintext_debug"] = True
    return item


def _session_plaintext(session: dict) -> dict:
    item = dict(session or {})
    item["plaintext_debug"] = True
    return item


def _debug_redaction_payload(*, account_id: Optional[str] = None, source: str = "admin_debug") -> dict:
    return {
        "redacted": False,
        "plaintext_debug": True,
        "plaintext_debug_source": source,
        "plaintext_debug_account_id": account_id,
    }


def _session_for_view(session: dict) -> dict:
    account_id = session.get("account_id") if session else None
    if _can_bypass_redaction_for_account(account_id):
        return _session_plaintext(session)
    return _redact_session(session)


def _profile_for_view(profile: dict, *, account_id: Optional[str]) -> dict:
    if _can_bypass_redaction_for_account(account_id):
        return _profile_plaintext(profile)
    return _redact_profile(profile)


def _platform_user_for_view(user: Optional[dict], *, account_id: Optional[str]) -> Optional[dict]:
    if user is None:
        return None
    if _can_bypass_redaction_for_account(account_id):
        item = dict(user)
        item["plaintext_debug"] = True
        return item
    return _redact_platform_user(user)


def _message_for_view(message: dict, *, account_id: Optional[str] = None) -> dict:
    target_account_id = account_id or message.get("account_id")
    if _can_bypass_redaction_for_account(target_account_id):
        return _message_plaintext(message)
    return _redact_message(message)


def _trace_for_view(trace: dict) -> dict:
    if _can_bypass_redaction_for_account(trace.get("account_id")):
        return _trace_plaintext(trace)
    return _redact_trace(trace)


def _redacted_flag_for_account(account_id: Optional[str]) -> bool:
    return not _can_bypass_redaction_for_account(account_id)


def _prompt_lab_session_for_account(
    *,
    account_id: str,
    session_id: Optional[int] = None,
) -> dict:
    """Return the selected account session for prompt-lab inspection."""
    if session_id is not None:
        session = get_session(session_id=session_id)
        if session is None or session.get("account_id") != account_id:
            raise HTTPException(status_code=404, detail="session not found")
        return session
    sessions = list_sessions_for_account(account_id=account_id, limit=20)
    if not sessions:
        raise HTTPException(status_code=404, detail="session not found")
    active = [
        session for session in sessions
        if session.get("session_key") == ACCOUNT_ACTIVE_SESSION_KEY and session.get("status") == "active"
    ]
    return active[0] if active else sessions[0]


def _validate_prompt_lab_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Validate replay messages for side-effect-free completion calls.

    tool role 消息和 assistant 的 tool_calls 字段在 replay 时跳过，
    避免工具调用历史导致 LLM API 400。
    """
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    cleaned: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        role = str((message or {}).get("role") or "").strip()
        if role == "tool":
            continue  # 工具调用结果跳过，replay 不需要
        content = str((message or {}).get("content") or "")
        if role not in {"system", "user", "assistant"}:
            raise HTTPException(
                status_code=400,
                detail=f"messages[{index}].role must be system, user, or assistant",
            )
        # assistant 有 tool_calls 但无 content 时跳过（纯工具调用轮）
        if role == "assistant" and not content and (message or {}).get("tool_calls"):
            continue
        cleaned.append({"role": role, "content": content})
    if not cleaned or cleaned[0]["role"] != "system":
        raise HTTPException(status_code=400, detail="messages[0].role must be system")
    return cleaned


def _prompt_lab_messages_for_view(*, account_id: str, messages: list[dict[str, Any]]) -> dict:
    if _can_bypass_redaction_for_account(account_id):
        return {
            "messages": messages,
            "system_prompt": messages[0]["content"] if messages else "",
            **_debug_redaction_payload(account_id=account_id),
        }
    return {
        "messages_redacted": True,
        "messages_count": len(messages),
        "messages_chars": sum(len(str(message.get("content") or "")) for message in messages),
        "system_prompt_redacted": True,
        "system_prompt_chars": len(str(messages[0].get("content") or "")) if messages else 0,
        "redacted": True,
    }


def _audit_plaintext_access(
    *,
    admin_user: dict,
    action: str,
    resource_type: str,
    resource_id: Optional[str],
    account_id: Optional[str],
    request_path: str,
    reason: Optional[str] = None,
    grant_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    insert_admin_access_event(
        admin_user_id=admin_user.get("id"),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        account_id=account_id,
        plaintext=True,
        grant_id=grant_id,
        reason=reason or "admin_plaintext_access",
        request_path=request_path,
        metadata=metadata,
    )


def _now_db_time() -> str:
    return beijing_now_str()


def _require_plaintext_access(
    *,
    admin_user: dict,
    account_id: str,
    resource_type: str,
) -> Optional[dict]:
    if admin_user.get("role") not in {"admin", "staff"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="plaintext access unavailable for role",
        )
    if admin_user.get("role") == "admin":
        return None
    grant = find_active_admin_plaintext_grant(
        requester_admin_user_id=str(admin_user.get("id")),
        account_id=account_id,
        resource_type=resource_type,
        now=_now_db_time(),
    )
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="plaintext grant required",
        )
    return grant


def _clean_scope_list(values: list[str], *, field_name: str) -> list[str]:
    result = []
    for value in values or []:
        text = str(value or "").strip()
        if text:
            result.append(text)
    if not result:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must include at least one value",
        )
    return list(dict.fromkeys(result))


__all__ = ['_normalize_optional_state_datetime', '_REDACTED_TEXT_FIELDS', '_METADATA_TEXT_FIELDS', '_content_meta', '_redact_raw_payload', '_redact_message', '_redact_trace', '_redact_profile', '_redact_session', '_redact_text_field', '_proactive_state_for_overview', '_proactive_message_settings_with_resolved', '_content_invitation_for_overview', '_BEIJING_TZ', '_beijing_display', '_TS_FIELDS', '_normalize_ts', '_content_invitation_for_reactivation_admin', '_list_reactivation_candidate_admin_items', '_redact_phone', '_redact_platform_user', '_binding_intent_for_view', '_debug_plaintext_account_allowlist', '_is_non_production_env', '_can_bypass_redaction_for_account', '_message_plaintext', '_trace_plaintext', '_profile_plaintext', '_session_plaintext', '_debug_redaction_payload', '_session_for_view', '_profile_for_view', '_platform_user_for_view', '_message_for_view', '_trace_for_view', '_redacted_flag_for_account', '_prompt_lab_session_for_account', '_validate_prompt_lab_messages', '_prompt_lab_messages_for_view', '_audit_plaintext_access', '_now_db_time', '_require_plaintext_access', '_clean_scope_list']
