import difflib
import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from app.config import settings
from app.time_utils import beijing_now
from app.db import (
    create_dreaming_run,
    get_dreaming_memory_item,
    get_session,
    insert_dreaming_memory_item,
    insert_memory_event,
    list_recent_messages,
    list_dreaming_memory_items,
    list_memory_events,
    update_dreaming_memory_item_status,
    update_dreaming_run,
    update_session_summary,
)
from app.agent_runtime.persistence import profile_storage
from app.user_profiles import (
    account_profile_dir,
    context_file_path,
    ensure_agent_context_files,
    read_context_file,
    write_context_file,
)

if TYPE_CHECKING:
    from app.agent_runtime.ports import MemorySink


logger = logging.getLogger("ai4all.dreaming")

DREAMING_PROMPT_VERSION = "dreaming_v1"

DREAMING_SYSTEM_PROMPT = """你是 AI4ALL 的 Dreaming 记忆整理器。

你的任务不是聊天，而是基于已发生的用户可见对话，为个人 AI 生成可审计的 session 压缩结果和长期记忆片段。

原则：
- 不要编造，只基于输入材料。
- 不要保存密码、验证码、token、身份证件、银行卡、精确地址或精确联系方式。
- 不要把一次性任务、寒暄、调试消息或失败回复当作长期记忆。
- 对医疗、法律、财务、政治、宗教、性取向等敏感内容要极其保守；除非用户明确要求长期记住，否则不要作为长期记忆片段。
- 输出必须是合法 JSON，不要输出解释，不要输出 Markdown。
"""

DREAMING_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["session_summary", "long_term_memory_items"],
    "properties": {
        "session_summary": {
            "type": "object",
            # 统一编排：rough_summary 停产，session_summary 收敛为单一 carryover_summary。
            # rough_summary 属性保留（可选）仅为兼容旧输出，规范化时会折叠进 carryover。
            "required": ["carryover_summary"],
            "properties": {
                "carryover_summary": {"type": "string"},
                "rough_summary": {"type": "string"},
                "open_threads": {"type": "array"},
                "tone_notes": {"type": "string"},
            },
        },
        "long_term_memory_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "fact_type",
                    "operation",
                    "target_file",
                    "category",
                    "memory_text",
                    "importance",
                    "confidence",
                    "sensitivity",
                    "reason",
                ],
                "properties": {
                    "fact_type": {
                        "type": "string",
                        "enum": [
                            "user_identity",
                            "user_preference",
                            "user_profile_derived",
                            "user_event",
                            "relationship",
                            "commitment",
                        ],
                    },
                    # 枚举值必须严格使用，便于下游自动应用判定（详见 prompt 说明）。
                    "operation": {
                        "type": "string",
                        "enum": ["add", "update", "delete", "downgrade"],
                    },
                    "target_file": {
                        "type": "string",
                        "enum": ["MEMORY.md", "USER.md", "SOUL.md", "IDENTITY.md"],
                    },
                    "category": {"type": "string"},
                    "memory_text": {"type": "string"},
                    "importance": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "confidence": {"type": "number"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["normal", "sensitive", "highly_sensitive"],
                    },
                    "reason": {"type": "string"},
                    "source_message_ids": {"type": "array"},
                    "source_daily_note_dates": {"type": "array"},
                },
            },
        },
        "excluded_sensitive_items": {"type": "array"},
    },
}

ALLOWED_TARGET_FILES = {"MEMORY.md", "USER.md", "SOUL.md", "IDENTITY.md"}
ALLOWED_OPERATIONS = {"add", "update", "delete", "downgrade"}
ALLOWED_IMPORTANCE = {"high", "medium", "low"}
ALLOWED_SENSITIVITY = {"normal", "sensitive", "highly_sensitive"}
ALLOWED_FACT_TYPES = {
    "user_identity",
    "user_preference",
    "user_profile_derived",
    "user_event",
    "relationship",
    "commitment",
}

# 上下文文件初始占位符行，写入真实记忆时应被替换掉（见 _append_memory_line）。
_MEMORY_PLACEHOLDER = "- 暂无"

TEXT_FIELD_RE = re.compile(
    r"(password|passwd|token|secret|验证码|密码|身份证|银行卡|住址|地址|手机号|电话|email|邮箱)",
    re.IGNORECASE,
)


def _memory_records_dir(account_id: str) -> Path:
    return account_profile_dir(account_id) / "memory"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).strip()


def _extract_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM output is not JSON")
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM output must be a JSON object")
    return payload


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _coerce_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _clean_text(item)
        if text:
            result.append(text)
    return result


def _normalize_dreaming_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    session_summary = payload.get("session_summary")
    if not isinstance(session_summary, dict):
        raise ValueError("session_summary must be an object")
    # 统一编排：carryover_summary 是唯一的 session 摘要产物。rough_summary 已停产，
    # 但兼容旧 LLM 输出——只给了 rough 时折叠成 carryover。
    carryover_summary = _clean_text(session_summary.get("carryover_summary"))
    if not carryover_summary:
        legacy_rough = _clean_text(session_summary.get("rough_summary"))
        if legacy_rough:
            carryover_summary = legacy_rough[:1200]
    if not carryover_summary:
        raise ValueError("session_summary requires carryover_summary")

    open_threads = session_summary.get("open_threads")
    if not isinstance(open_threads, list):
        open_threads = []
    normalized_summary = {
        "carryover_summary": carryover_summary,
        "open_threads": open_threads[:12],
        "tone_notes": _clean_text(session_summary.get("tone_notes")),
    }

    raw_items = payload.get("long_term_memory_items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError("long_term_memory_items must be an array")

    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        memory_text = _clean_text(raw_item.get("memory_text"))
        if not memory_text:
            continue
        operation = _clean_text(raw_item.get("operation")).lower() or "add"
        if operation not in ALLOWED_OPERATIONS:
            operation = "add"
        target_file = _clean_text(raw_item.get("target_file")) or "MEMORY.md"
        if target_file not in ALLOWED_TARGET_FILES:
            target_file = "MEMORY.md"
        importance = _clean_text(raw_item.get("importance")).lower() or "medium"
        if importance not in ALLOWED_IMPORTANCE:
            importance = "medium"
        # sensitivity 必须落在 ALLOWED_SENSITIVITY；模型若返回非法值（历史 bug：
        # 未在 prompt/schema 告知枚举，导致模型给出"低/无/none"等被全部兜底成
        # sensitive，进而被自动应用阶段全部 skip），这里兜底成 normal，并由
        # _auto_apply_skip_reason 中的 TEXT_FIELD_RE 作为 PII 最后防线。
        raw_sensitivity = _clean_text(raw_item.get("sensitivity")).lower()
        sensitivity = raw_sensitivity or "normal"
        if sensitivity not in ALLOWED_SENSITIVITY:
            logger.warning(
                "dreaming: 非法 sensitivity 值 %r，兜底为 normal", raw_sensitivity
            )
            sensitivity = "normal"
        category = _clean_text(raw_item.get("category")).lower() or "other"
        raw_fact_type = _clean_text(raw_item.get("fact_type")).lower()
        fact_type = raw_fact_type
        if fact_type not in ALLOWED_FACT_TYPES:
            # 兼容旧/异常模型输出：per-account 记忆仍按原规则应用，但共享路由会拒绝
            # unclassified，避免缺少产品评审的新类型误进 L3。
            fact_type = "unclassified"
            logger.warning(
                "dreaming: 缺失或非法 fact_type %r，L3 路由将 fail-closed",
                raw_fact_type,
            )
        reason = _clean_text(raw_item.get("reason"))
        source_message_ids = _coerce_string_list(raw_item.get("source_message_ids"))
        source_daily_note_dates = _coerce_string_list(raw_item.get("source_daily_note_dates"))
        items.append(
            {
                "fact_type": fact_type,
                "operation": operation,
                "target_file": target_file,
                "category": category[:80],
                "memory_text": memory_text,
                "importance": importance,
                "confidence": _coerce_confidence(raw_item.get("confidence")),
                "sensitivity": sensitivity,
                "reason": reason,
                "source_message_ids": source_message_ids,
                "source_daily_note_dates": source_daily_note_dates,
            }
        )

    excluded = payload.get("excluded_sensitive_items")
    if not isinstance(excluded, list):
        excluded = []

    return {
        "session_summary": normalized_summary,
        "long_term_memory_items": items,
        "excluded_sensitive_items": excluded[:50],
    }


def list_recent_daily_memory(
    *,
    account_id: str,
    today: str,
    days: int = 7,
) -> List[Dict[str, object]]:
    """Return non-empty memory/YYYY-MM-DD.md records, oldest to newest."""
    days = max(1, min(days, 30))
    today_date = date.fromisoformat(today)
    base = _memory_records_dir(account_id)
    records: List[Dict[str, object]] = []
    for offset in range(days - 1, -1, -1):
        date_str = (today_date - timedelta(days=offset)).isoformat()
        raw = profile_storage.read_file(account_id, f"memory/{date_str}.md")
        if raw is None:
            continue
        content = raw.strip()
        if not content:
            continue
        records.append(
            {
                "date": date_str,
                "path": str(base / f"{date_str}.md"),  # 逻辑路径，仅展示
                "chars": len(content),
                "content": content,
            }
        )
    return records


def _format_daily_notes(records: List[Dict[str, object]]) -> str:
    parts = []
    for record in records:
        parts.append(f"## {record['date']}\n{record['content']}")
    return "\n\n".join(parts)


def _format_session_messages(messages: List[Dict[str, str]]) -> str:
    parts = []
    for index, message in enumerate(messages, start=1):
        role = "User" if message.get("role") == "user" else "AI"
        content = str(message.get("content") or "").strip()
        if content:
            parts.append(f"{index}. {role}: {content}")
    return "\n".join(parts)


def _strip_context_heading(text: str, heading: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().lower() == f"# {heading}".lower():
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines).strip()


def _strip_memory_heading(text: str) -> str:
    return _strip_context_heading(text, "MEMORY")


def read_long_term_memory(account_id: str) -> str:
    ensure_agent_context_files(account_id)
    raw = read_context_file(account_id, "MEMORY.md")
    if raw is None:
        return ""
    return _strip_memory_heading(raw)


def write_long_term_memory(account_id: str, memory_body: str) -> Path:
    ensure_agent_context_files(account_id)
    write_context_file(account_id, "MEMORY.md", "# MEMORY\n\n" + memory_body.strip() + "\n")
    return context_file_path(account_id, "MEMORY.md")


def _read_context_file(account_id: str, target_file: str) -> str:
    ensure_agent_context_files(account_id)
    # ensure_agent_context_files 保证目标文件已存在；缺失兜底成空串。
    return read_context_file(account_id, target_file) or ""


def _write_context_file(account_id: str, target_file: str, content: str) -> Path:
    ensure_agent_context_files(account_id)
    write_context_file(account_id, target_file, content.rstrip() + "\n")
    return context_file_path(account_id, target_file)


def _build_dreaming_prompt(
    *,
    source_type: str,
    current_memory: str,
    current_user: str,
    session_metadata: Dict[str, Any],
    session_messages: str,
    daily_notes: str,
) -> str:
    return f"""请根据以下材料完成 Dreaming 压缩。

触发类型：
{source_type}

目标：
1. 生成新 session 可直接承接使用的 carryover_summary（旧 session 的唯一摘要）。
2. 排除敏感信息后，提取需要长期记住的用户方面信息，作为 long_term_memory_items。

输出要求：
- 只输出 JSON。
- carryover_summary 要忠于材料、要短、可直接注入后续聊天 prompt。
- long_term_memory_items 必须克制、去重、可追溯。
- 如果没有长期记忆片段，输出空数组。

每个 long_term_memory_items 元素的枚举字段必须严格使用以下英文取值，不要翻译、不要自创：
- fact_type：
  - user_identity：真人称呼、身份、基本信息
  - user_preference：偏好、口味、禁忌
  - user_profile_derived：派生画像、稳定性格或关系网认知
  - user_event：关于用户的客观事件或里程碑（不是聊天原文）
  - relationship：只属于当前 AI 与用户的关系阶段或共同历史
  - commitment：当前 AI 对用户作出的承诺
- operation：add | update | delete | downgrade
- target_file：MEMORY.md（通用长期记忆）| USER.md（用户称呼/身份/偏好）| SOUL.md | IDENTITY.md
- importance：high | medium | low
- sensitivity：normal | sensitive | highly_sensitive
  - normal：称呼、昵称、AI 名字与性格设定、日常偏好、稳定身份等可长期保存的普通信息。
  - sensitive：医疗、法律、财务、政治、宗教、性取向等敏感话题。
  - highly_sensitive：密码、验证码、证件号、银行卡、精确住址或精确联系方式（这类本就不应提取为长期记忆）。
  绝大多数可长期记住的偏好与称呼都应标为 normal；只有真正触及上述敏感/高度敏感类别时才上调。

当前 MEMORY.md：
{current_memory or "- 暂无"}

当前 USER.md：
{current_user or "- 暂无"}

旧 session metadata：
{json.dumps(session_metadata, ensure_ascii=False, indent=2)}

旧 session 可见对话：
{session_messages or "- 无"}

业务日 daily notes：
{daily_notes or "- 无"}

请按以下 JSON schema 输出：
{json.dumps(DREAMING_JSON_SCHEMA, ensure_ascii=False, indent=2)}
"""


def _call_dreaming_llm(
    *,
    source_type: str,
    current_memory: str,
    current_user: str,
    session_metadata: Dict[str, Any],
    session_messages: str,
    daily_notes: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Optional[int]]]]:
    from app.agent_runtime.llm.service import generate_completion_with_usage, is_llm_configured
    from app.agent_runtime.llm.providers import TASK_DREAMING, tier_for_task

    tier = tier_for_task(TASK_DREAMING)
    if not is_llm_configured(tier):
        raise RuntimeError("llm_disabled")

    messages = [
        {"role": "system", "content": DREAMING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_dreaming_prompt(
                source_type=source_type,
                current_memory=current_memory,
                current_user=current_user,
                session_metadata=session_metadata,
                session_messages=session_messages,
                daily_notes=daily_notes,
            ),
        },
    ]
    raw, usage = generate_completion_with_usage(messages, tier=tier)
    return _normalize_dreaming_payload(_extract_json_object(raw)), usage


def _deterministic_payload(
    *,
    messages: List[Dict[str, str]],
    daily_notes: str,
) -> Dict[str, Any]:
    lines = []
    for message in messages[-8:]:
        role = "用户" if message.get("role") == "user" else "AI"
        content = str(message.get("content") or "").strip()
        if len(content) > 220:
            content = content[:220] + "...[truncated]"
        if content:
            lines.append(f"{role}: {content}")
    if not lines and daily_notes:
        compact_notes = daily_notes[:1200]
        lines.append(f"daily notes 摘要材料: {compact_notes}")
    fallback = "\n".join(lines).strip()
    if fallback:
        carryover_summary = f"Recent carryover from previous session:\n{fallback}"
    else:
        carryover_summary = ""
    return {
        "session_summary": {
            "carryover_summary": carryover_summary,
            "open_threads": [],
            "tone_notes": "",
        },
        "long_term_memory_items": [],
        "excluded_sensitive_items": [],
    }


def _diff_text(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )


def _append_memory_line(content: str, target_file: str, memory_text: str) -> str:
    line = memory_text.strip()
    if not line.startswith("- "):
        line = f"- {line}"
    # 写入真实记忆时清掉占位符行（"- 暂无"），避免与真实条目并存造成自相矛盾，
    # 也避免占位符进入 prompt。
    kept = [ln for ln in content.splitlines() if ln.strip() != _MEMORY_PLACEHOLDER]
    stripped = "\n".join(kept).rstrip()
    heading = f"# {target_file[:-3]}"
    if not stripped or stripped == heading:
        return f"{heading}\n\n{line}\n"
    if line in stripped.splitlines():
        return stripped + "\n"
    return f"{stripped}\n{line}\n"


def _remove_memory_line(content: str, memory_text: str) -> str:
    target = memory_text.strip()
    target_without_bullet = target[2:].strip() if target.startswith("- ") else target
    next_lines = []
    removed = False
    for line in content.splitlines():
        normalized = line.strip()
        normalized_without_bullet = (
            normalized[2:].strip() if normalized.startswith("- ") else normalized
        )
        if normalized_without_bullet == target_without_bullet:
            removed = True
            continue
        next_lines.append(line)
    if not removed:
        return content.rstrip() + "\n"
    return "\n".join(next_lines).rstrip() + "\n"


def _auto_apply_skip_reason(item: Dict[str, Any], *, source_type: str) -> Optional[str]:
    if item["sensitivity"] != "normal":
        return "sensitive_item"
    if item["confidence"] < 0.75:
        return "low_confidence"
    if item["importance"] == "low":
        return "low_importance"
    if item["target_file"] not in ALLOWED_TARGET_FILES:
        return "unsupported_target_file"
    if item["operation"] in {"delete", "downgrade"} and item["category"] != "correction":
        return "destructive_operation_requires_correction"
    if TEXT_FIELD_RE.search(item["memory_text"]):
        return "potentially_sensitive_text"
    return None


def _apply_memory_item(
    item: Dict[str, Any],
    *,
    actor_type: str,
    actor_id: Optional[str],
) -> Dict[str, Any]:
    skip_reason = _auto_apply_skip_reason(item, source_type=str(item["source_type"]))
    if skip_reason:
        updated = update_dreaming_memory_item_status(
            item_id=int(item["id"]),
            apply_status="skipped",
            skip_reason=skip_reason,
        )
        insert_memory_event(
            account_id=item["account_id"],
            memory_item_id=int(item["id"]),
            event_type="skipped",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"skip_reason": skip_reason},
        )
        return updated or item

    try:
        before = _read_context_file(item["account_id"], item["target_file"])
        before_hash = _sha256_text(before)
        if item["operation"] in {"delete", "downgrade"}:
            after = _remove_memory_line(before, item["memory_text"])
        else:
            after = _append_memory_line(before, item["target_file"], item["memory_text"])
        diff = _diff_text(before, after)
        if before == after:
            updated = update_dreaming_memory_item_status(
                item_id=int(item["id"]),
                apply_status="skipped",
                skip_reason="duplicate_or_noop",
                base_text_hash=before_hash,
                diff={"target_file": item["target_file"], "diff_text": ""},
            )
            insert_memory_event(
                account_id=item["account_id"],
                memory_item_id=int(item["id"]),
                event_type="skipped",
                actor_type=actor_type,
                actor_id=actor_id,
                metadata={"skip_reason": "duplicate_or_noop"},
            )
            return updated or item
        _write_context_file(item["account_id"], item["target_file"], after)
        updated = update_dreaming_memory_item_status(
            item_id=int(item["id"]),
            apply_status="applied",
            skip_reason=None,
            base_text_hash=before_hash,
            diff={"target_file": item["target_file"], "diff_text": diff},
            applied=True,
        )
        insert_memory_event(
            account_id=item["account_id"],
            memory_item_id=int(item["id"]),
            event_type="applied",
            actor_type=actor_type,
            actor_id=actor_id,
            before_text=before,
            after_text=after,
            diff_text=diff,
            metadata={"target_file": item["target_file"], "base_text_hash": before_hash},
        )
        return updated or item
    except Exception as exc:
        logger.exception("dreaming apply failed item=%s error=%s", item.get("id"), exc)
        updated = update_dreaming_memory_item_status(
            item_id=int(item["id"]),
            apply_status="failed",
            skip_reason=str(exc),
        )
        insert_memory_event(
            account_id=item["account_id"],
            memory_item_id=int(item["id"]),
            event_type="failed",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={"error": str(exc)},
        )
        return updated or item


def _create_items_from_payload(
    *,
    account_id: str,
    run_id: int,
    source_type: str,
    source_session_id: Optional[int],
    payload: Dict[str, Any],
    actor_type: str,
    actor_id: Optional[str],
    memory_sink: Optional["MemorySink"],
    source_business_day: Optional[str],
) -> List[Dict[str, Any]]:
    created: List[Dict[str, Any]] = []
    for item in payload.get("long_term_memory_items", []):
        source_daily_note_dates = item.get("source_daily_note_dates") or []
        source_daily_note_date = source_daily_note_dates[0] if source_daily_note_dates else None
        db_item = insert_dreaming_memory_item(
            account_id=account_id,
            dreaming_run_id=run_id,
            source_type=source_type,
            source_session_id=source_session_id,
            source_daily_note_date=source_daily_note_date,
            operation=item["operation"],
            target_file=item["target_file"],
            category=item["category"],
            memory_text=item["memory_text"],
            importance=item["importance"],
            confidence=float(item["confidence"]),
            sensitivity=item["sensitivity"],
            reason=item.get("reason"),
            metadata={
                "fact_type": item["fact_type"],
                "source_message_ids": item.get("source_message_ids") or [],
                "source_daily_note_dates": source_daily_note_dates,
            },
        )
        insert_memory_event(
            account_id=account_id,
            memory_item_id=int(db_item["id"]),
            event_type="generated",
            actor_type=actor_type,
            actor_id=actor_id,
            metadata={
                "dreaming_run_id": run_id,
                "category": db_item["category"],
                "importance": db_item["importance"],
                "confidence": db_item["confidence"],
                "sensitivity": db_item["sensitivity"],
            },
        )
        applied_item = _apply_memory_item(
            db_item, actor_type=actor_type, actor_id=actor_id
        )
        _emit_memory_item(
            applied_item,
            memory_sink=memory_sink,
            source_business_day=source_business_day,
        )
        created.append(applied_item)
    return created


def _emit_memory_item(
    item: Dict[str, Any],
    *,
    memory_sink: Optional["MemorySink"],
    source_business_day: Optional[str],
) -> None:
    """把已应用的蒸馏事实发往可选 typed sink；sink 失败不回滚 per-account 记忆。"""
    if memory_sink is None or item.get("apply_status") != "applied":
        return
    from app.agent_runtime.ports import MemoryEvent, MemoryProvenance

    metadata = item.get("metadata") or {}
    source_message_ids = metadata.get("source_message_ids") or []
    now = beijing_now()
    event = MemoryEvent(
        fact_type=str(metadata.get("fact_type") or "unclassified"),
        payload={
            "memory_text": str(item["memory_text"]),
            "operation": str(item["operation"]),
            "target_file": str(item["target_file"]),
            "category": str(item["category"]),
        },
        provenance=MemoryProvenance(
            source_account_id=str(item["account_id"]),
            turn_message_id=(
                str(source_message_ids[0]) if source_message_ids else None
            ),
            session_id=(
                int(item["source_session_id"])
                if item.get("source_session_id") is not None
                else None
            ),
            business_day=str(
                source_business_day
                or item.get("source_daily_note_date")
                or now.date().isoformat()
            ),
            occurred_at=now.isoformat(timespec="seconds"),
        ),
    )
    try:
        memory_sink.emit(event)
    except Exception as exc:
        logger.exception(
            "dreaming typed memory sink failed account=%s item=%s fact_type=%s error=%s",
            item.get("account_id"),
            item.get("id"),
            event.fact_type,
            exc,
        )


def _source_files_metadata(records: List[Dict[str, object]]) -> List[Dict[str, Any]]:
    return [
        {
            "date": str(record["date"]),
            "path": str(record["path"]),
            "chars": int(record["chars"]),
        }
        for record in records
    ]


def _load_context_for_dreaming(
    *,
    account_id: str,
    today: str,
    days: int,
    source_session_id: Optional[int],
    source_business_day: Optional[str],
) -> Tuple[Dict[str, Any], List[Dict[str, str]], List[Dict[str, object]], str, str, str]:
    ensure_agent_context_files(account_id)
    records = list_recent_daily_memory(account_id=account_id, today=today, days=days)
    daily_notes = _format_daily_notes(records)
    current_memory = _read_context_file(account_id, "MEMORY.md")
    current_user = _read_context_file(account_id, "USER.md")

    session_metadata: Dict[str, Any] = {}
    messages: List[Dict[str, str]] = []
    if source_session_id is not None:
        session = get_session(session_id=source_session_id)
        if session:
            session_metadata = {
                "id": session.get("id"),
                "account_id": session.get("account_id"),
                "status": session.get("status"),
                "turn_count": session.get("turn_count"),
                "business_day": session.get("business_day"),
                "source_business_day": source_business_day,
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
            }
            limit = max(32, int(session.get("turn_count") or 0) * 2 + 16)
            messages = list_recent_messages(session_id=source_session_id, limit=limit)
    else:
        session_metadata = {
            "account_id": account_id,
            "source_business_day": source_business_day,
        }
    return (
        session_metadata,
        messages,
        records,
        current_memory,
        current_user,
        daily_notes,
    )


def run_dreaming(
    *,
    account_id: str,
    today: Optional[str] = None,
    days: int = 7,
    source_type: str = "manual_admin",
    source_session_id: Optional[int] = None,
    source_business_day: Optional[str] = None,
    actor_type: str = "admin",
    actor_id: Optional[str] = None,
    allow_fallback: bool = False,
    memory_sink: Optional["MemorySink"] = None,
) -> Dict[str, object]:
    """Run Dreaming, apply per-account memory, and emit eligible distilled facts."""
    from app.agent_runtime.llm.service import get_active_llm_model
    from app.agent_runtime.llm.providers import TASK_DREAMING, tier_for_task

    today = today or date.today().isoformat()
    days = max(1, min(days, 30))
    active_llm_model = get_active_llm_model(tier_for_task(TASK_DREAMING))

    (
        session_metadata,
        messages,
        records,
        current_memory,
        current_user,
        daily_notes,
    ) = _load_context_for_dreaming(
        account_id=account_id,
        today=today,
        days=days,
        source_session_id=source_session_id,
        source_business_day=source_business_day,
    )
    source_files = _source_files_metadata(records)
    memory_path = context_file_path(account_id, "MEMORY.md")
    session_messages_text = _format_session_messages(messages)

    if not records and not messages:
        run = create_dreaming_run(
            account_id=account_id,
            source_type=source_type,
            source_session_id=source_session_id,
            source_business_day=source_business_day,
            status="succeeded",
            prompt_version=DREAMING_PROMPT_VERSION,
            llm_model=active_llm_model,
            input_hash=None,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        update_dreaming_run(
            run_id=int(run["id"]),
            status="succeeded",
            output={
                "session_summary": {
                    "carryover_summary": "",
                    "open_threads": [],
                    "tone_notes": "",
                },
                "long_term_memory_items": [],
                "excluded_sensitive_items": [],
                "reason": "no_source_material",
            },
            completed=True,
        )
        return {
            "status": "skipped",
            "reason": "no_source_material",
            "account_id": account_id,
            "run_id": run["id"],
            "today": today,
            "days": days,
            "source_files": source_files,
            "memory_path": str(memory_path),
            "memory_chars": len(read_long_term_memory(account_id)),
            "items": [],
        }

    prompt_input = json.dumps(
        {
            "source_type": source_type,
            "session_metadata": session_metadata,
            "session_messages": session_messages_text,
            "daily_notes": daily_notes,
            "current_memory": current_memory,
            "current_user": current_user,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    input_hash = _sha256_text(prompt_input)
    run = create_dreaming_run(
        account_id=account_id,
        source_type=source_type,
        source_session_id=source_session_id,
        source_business_day=source_business_day,
        status="running",
        prompt_version=DREAMING_PROMPT_VERSION,
        llm_model=active_llm_model,
        input_hash=input_hash,
        actor_type=actor_type,
        actor_id=actor_id,
    )

    used_fallback = False
    token_usage: Optional[Dict[str, Optional[int]]] = None
    try:
        payload, token_usage = _call_dreaming_llm(
            source_type=source_type,
            current_memory=current_memory,
            current_user=current_user,
            session_metadata=session_metadata,
            session_messages=session_messages_text,
            daily_notes=daily_notes,
        )
    except Exception as exc:
        logger.warning("dreaming LLM failed account=%s error=%s", account_id, exc)
        if not allow_fallback:
            update_dreaming_run(
                run_id=int(run["id"]),
                status="failed",
                output={},
                error=str(exc),
                completed=True,
            )
            return {
                "status": "failed",
                "reason": "llm_failed",
                "error": str(exc),
                "account_id": account_id,
                "run_id": run["id"],
                "today": today,
                "days": days,
                "source_files": source_files,
                "memory_path": str(memory_path),
                "memory_chars": len(read_long_term_memory(account_id)),
                "items": [],
            }
        payload = _deterministic_payload(messages=messages, daily_notes=daily_notes)
        used_fallback = True

    summary = payload["session_summary"]
    if source_session_id is not None:
        update_session_summary(
            session_id=source_session_id,
            # rough_summary 停产后，session_summary 落库列统一写 carryover（单一摘要）。
            session_summary=summary.get("carryover_summary"),
            carryover_summary=summary.get("carryover_summary"),
            summary_model=(active_llm_model if not used_fallback else "deterministic_fallback"),
            summary_prompt_version=DREAMING_PROMPT_VERSION,
        )

    items = _create_items_from_payload(
        account_id=account_id,
        run_id=int(run["id"]),
        source_type=source_type,
        source_session_id=source_session_id,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
        memory_sink=memory_sink,
        source_business_day=source_business_day,
    )
    applied_count = sum(1 for item in items if item.get("apply_status") == "applied")
    skipped_count = sum(1 for item in items if item.get("apply_status") == "skipped")
    failed_count = sum(1 for item in items if item.get("apply_status") == "failed")
    run_status = "partial" if failed_count else "succeeded"
    update_dreaming_run(
        run_id=int(run["id"]),
        status=run_status,
        output=payload,
        error="llm_failed_fallback_used" if used_fallback else None,
        token_input=(token_usage or {}).get("input"),
        token_output=(token_usage or {}).get("output"),
        completed=True,
    )

    return {
        "status": "updated" if applied_count else "succeeded",
        "reason": "fallback_used" if used_fallback else None,
        "account_id": account_id,
        "run_id": run["id"],
        "today": today,
        "days": days,
        "source_type": source_type,
        "source_session_id": source_session_id,
        "source_business_day": source_business_day,
        "source_files": source_files,
        "memory_path": str(memory_path),
        "memory_chars": len(read_long_term_memory(account_id)),
        "session_summary": summary,
        "item_count": len(items),
        "applied_count": applied_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "items": items,
    }


def reapply_sensitivity_misskips(
    *,
    account_id: Optional[str] = None,
    apply: bool = False,
    limit: int = 500,
    actor_id: str = "backfill_sensitivity_fix",
) -> Dict[str, Any]:
    """回填历史 bug 误判为敏感而被 skip 的长期记忆条目。

    历史 bug：prompt/schema 未告知模型 sensitivity 枚举，模型返回的非法值被兜底成
    "sensitive"，导致所有条目在自动应用阶段以 skip_reason='sensitive_item' 被丢弃。
    本函数把这些条目的 sensitivity 重置为 "normal" 后，按现行 _auto_apply_skip_reason
    规则重新评估；TEXT_FIELD_RE（PII 后盾）与置信度/重要性门槛仍然生效，因此真正含
    密码/银行卡/手机号等的条目仍会被拦下。

    apply=False（默认）：仅预演，不写文件、不改 DB。
    apply=True：调用 _apply_memory_item 真正写入 MEMORY.md/USER.md 并记录审计事件。
    """
    candidates = [
        item
        for item in list_dreaming_memory_items(
            account_id=account_id, apply_status="skipped", limit=limit
        )
        if item.get("skip_reason") == "sensitive_item"
    ]
    decisions: List[Dict[str, Any]] = []
    applied = would_apply = skipped = 0
    for item in candidates:
        eval_item = dict(item)
        eval_item["sensitivity"] = "normal"
        if apply:
            result = _apply_memory_item(
                eval_item, actor_type="system", actor_id=actor_id
            )
            status = str(result.get("apply_status"))
            reason = result.get("skip_reason")
            if status == "applied":
                applied += 1
            else:
                skipped += 1
        else:
            reason = _auto_apply_skip_reason(
                eval_item, source_type=str(eval_item.get("source_type"))
            )
            status = "would_skip" if reason else "would_apply"
            if reason:
                skipped += 1
            else:
                would_apply += 1
        decisions.append(
            {
                "id": item["id"],
                "account_id": item["account_id"],
                "target_file": item["target_file"],
                "importance": item["importance"],
                "confidence": item["confidence"],
                "status": status,
                "skip_reason": reason,
                "memory_text": item["memory_text"],
            }
        )
    return {
        "apply": apply,
        "candidates": len(candidates),
        "applied": applied,
        "would_apply": would_apply,
        "skipped": skipped,
        "decisions": decisions,
    }


def rollback_memory_item(
    *,
    item_id: int,
    actor_type: str = "admin",
    actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    item = get_dreaming_memory_item(item_id=item_id)
    if item is None:
        return {"status": "not_found", "item_id": item_id}
    if item.get("apply_status") != "applied":
        return {"status": "skipped", "reason": "item_not_applied", "item": item}

    events = list_memory_events(memory_item_id=item_id, limit=20)
    applied = next((event for event in events if event["event_type"] == "applied"), None)
    if applied is None:
        return {"status": "failed", "reason": "applied_event_missing", "item": item}

    target_file = item["target_file"]
    current = _read_context_file(item["account_id"], target_file)
    after_text = applied.get("after_text") or ""
    before_text = applied.get("before_text") or ""
    if current.rstrip() != after_text.rstrip():
        update_dreaming_memory_item_status(
            item_id=item_id,
            apply_status="failed",
            skip_reason="rollback_target_changed",
        )
        return {"status": "failed", "reason": "rollback_target_changed", "item": item}

    _write_context_file(item["account_id"], target_file, before_text)
    diff = _diff_text(current, before_text)
    updated = update_dreaming_memory_item_status(
        item_id=item_id,
        apply_status="rolled_back",
        skip_reason=None,
        diff={"target_file": target_file, "rollback_diff_text": diff},
    )
    insert_memory_event(
        account_id=item["account_id"],
        memory_item_id=item_id,
        event_type="rollback",
        actor_type=actor_type,
        actor_id=actor_id,
        before_text=current,
        after_text=before_text,
        diff_text=diff,
        metadata={"target_file": target_file},
    )
    return {"status": "rolled_back", "item": updated}


def redact_text(text: Optional[str], *, max_chars: int = 80) -> str:
    if not text:
        return ""
    value = str(text)
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[redacted-email]", value)
    value = re.sub(r"1[3-9]\d{9}", "[redacted-phone]", value)
    value = re.sub(r"\b\d{6,}\b", "[redacted-number]", value)
    if len(value) > max_chars:
        value = value[:max_chars] + "...[redacted]"
    return value


def summarize_dreaming_run_for_debug(run: Dict[str, Any]) -> Dict[str, Any]:
    output = run.get("output") or {}
    summary = output.get("session_summary") if isinstance(output, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "id": run["id"],
        "account_id": run["account_id"],
        "source_type": run["source_type"],
        "source_session_id": run.get("source_session_id"),
        "source_business_day": run.get("source_business_day"),
        "status": run["status"],
        "prompt_version": run["prompt_version"],
        "llm_model": run.get("llm_model"),
        "input_hash": run.get("input_hash"),
        "error": run.get("error"),
        "carryover_summary_preview": redact_text(summary.get("carryover_summary"), max_chars=120),
        "created_at": run.get("created_at"),
        "completed_at": run.get("completed_at"),
    }


def summarize_memory_item_for_debug(item: Dict[str, Any]) -> Dict[str, Any]:
    diff = item.get("diff") or {}
    diff_text = diff.get("diff_text") or diff.get("rollback_diff_text") or ""
    return {
        "id": item["id"],
        "account_id": item["account_id"],
        "dreaming_run_id": item["dreaming_run_id"],
        "source_type": item["source_type"],
        "source_session_id": item.get("source_session_id"),
        "source_daily_note_date": item.get("source_daily_note_date"),
        "operation": item["operation"],
        "target_file": item["target_file"],
        "category": item["category"],
        "memory_text_preview": redact_text(item.get("memory_text"), max_chars=120),
        "importance": item["importance"],
        "confidence": item["confidence"],
        "sensitivity": item["sensitivity"],
        "apply_status": item["apply_status"],
        "skip_reason": item.get("skip_reason"),
        "reason_preview": redact_text(item.get("reason"), max_chars=120),
        "diff_summary": {
            "chars": len(diff_text),
            "preview": redact_text(diff_text, max_chars=160),
        },
        "created_at": item.get("created_at"),
        "applied_at": item.get("applied_at"),
    }
