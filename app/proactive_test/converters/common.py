from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCENARIO_TYPES = ("account_check", "reactivation_topic", "content_invitation")
DEFAULT_SILENCE_HOURS = {
    "account_check": 24,
    "reactivation_topic": 72,
    "content_invitation": 48,
}

CONTENT_KEYWORDS = (
    "资料", "文章", "新闻", "攻略", "教程", "推荐", "研究", "了解", "学习",
    "paper", "news", "guide", "tutorial", "recommend",
)
ACCOUNT_CHECK_KEYWORDS = (
    "明天", "后天", "下周", "周末", "今晚", "等会", "下午", "面试", "见面",
    "开会", "运动", "考试", "旅行", "appointment", "tomorrow", "next week",
    "interview", "meeting", "exam", "workout",
)


@dataclass
class DatasetResult:
    dataset: str
    loaded: int = 0
    converted: int = 0
    skipped: int = 0
    downloaded: Optional[bool] = None
    reason: Optional[str] = None
    license_reminders: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as err:
                raise ValueError(f"{path}:{line_no}: {err}") from err
            if isinstance(item, dict):
                rows.append(item)
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def infer_scenario_type(text: str, allowed: Tuple[str, ...] = SCENARIO_TYPES) -> str:
    lowered = str(text or "").lower()
    if "content_invitation" in allowed and any(keyword.lower() in lowered for keyword in CONTENT_KEYWORDS):
        return "content_invitation"
    if "account_check" in allowed and any(keyword.lower() in lowered for keyword in ACCOUNT_CHECK_KEYWORDS):
        return "account_check"
    if "reactivation_topic" in allowed:
        return "reactivation_topic"
    return allowed[0] if allowed else "reactivation_topic"


def silence_for(
    scenario_type: str,
    *,
    default_silence_hours: Optional[float] = None,
) -> float:
    if default_silence_hours is not None:
        return float(default_silence_hours)
    return float(DEFAULT_SILENCE_HOURS.get(scenario_type, 72))


def normalize_chat_history(
    messages: List[Dict[str, Any]],
    *,
    max_turns: int = 8,
    max_message_chars: int = 1000,
    max_total_chars: int = 8000,
) -> Tuple[List[Dict[str, str]], List[str]]:
    notes: List[str] = []
    normalized: List[Dict[str, str]] = []
    total = 0
    for idx, item in enumerate(messages[: max(1, max_turns)]):
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        if len(text) > max_message_chars:
            text = text[:max_message_chars]
            notes.append("truncated")
        if total + len(text) > max_total_chars:
            remaining = max_total_chars - total
            if remaining <= 0:
                notes.append("truncated")
                break
            text = text[:remaining]
            notes.append("truncated")
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant", "system"}:
            role = "user" if len(normalized) % 2 == 0 else "assistant"
        normalized.append({"role": role, "text": text})
        total += len(text)
    if len(normalized) < 4:
        notes.append("short_dialogue")
    return normalized, list(dict.fromkeys(notes))


def sample_dialogues(
    rows: List[Any],
    *,
    limit: int,
    seed: int,
) -> List[Any]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    rng = random.Random(seed)
    return rng.sample(rows, limit)


def make_sample(
    *,
    dataset_name: str,
    index: int,
    source: str,
    chat_history: List[Dict[str, str]],
    scenario_types: Tuple[str, ...],
    default_silence_hours: Optional[float],
    notes: str,
) -> Dict[str, Any]:
    joined = "\n".join(item.get("text", "") for item in chat_history)
    scenario = infer_scenario_type(joined, scenario_types)
    return {
        "sample_id": f"{dataset_name}_{index:06d}",
        "source": source,
        "dataset_name": dataset_name,
        "scenario_type": scenario,
        "chat_history": chat_history,
        "silence_hours": silence_for(scenario, default_silence_hours=default_silence_hours),
        "notes": notes,
    }
