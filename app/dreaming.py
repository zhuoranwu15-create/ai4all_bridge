import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from app.config import settings
from app.user_profiles import account_profile_dir, context_file_path, ensure_agent_context_files


logger = logging.getLogger("ai4all.dreaming")

DISTILLATION_PROMPT = """从最近的 daily notes 中蒸馏长期记忆，并合并到当前 MEMORY.md。

目标：
- 保留对未来对话长期有用的信息
- 去重，合并相似条目
- 删除短期、重复、过时或没有长期价值的内容
- 重点保留：用户偏好、长期身份信息、重要经历、长期目标、对 AI 的明确要求
- 不要保留调试消息、一次性测试、纯粹寒暄或明显失败回复

输出要求：
- 只输出新的 MEMORY.md 正文
- 每条用 "- " 开头
- 不要输出解释，不要输出代码块
- 如果没有值得保留的长期记忆，输出 NOTHING

当前 MEMORY.md：
{current_memory}

最近 daily notes：
{daily_notes}
"""


def _memory_records_dir(account_id: str) -> Path:
    return account_profile_dir(account_id) / "memory"


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
        path = base / f"{date_str}.md"
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        records.append(
            {
                "date": date_str,
                "path": str(path),
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


def _strip_memory_heading(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip().lower() == "# memory":
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines).strip()


def read_long_term_memory(account_id: str) -> str:
    ensure_agent_context_files(account_id)
    path = context_file_path(account_id, "MEMORY.md")
    if not path.exists():
        return ""
    return _strip_memory_heading(path.read_text(encoding="utf-8"))


def write_long_term_memory(account_id: str, memory_body: str) -> Path:
    ensure_agent_context_files(account_id)
    path = context_file_path(account_id, "MEMORY.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# MEMORY\n\n" + memory_body.strip() + "\n", encoding="utf-8")
    return path


def _normalize_memory_body(text: str) -> str:
    body = _strip_memory_heading(text)
    return body.strip()


def _is_empty_distillation(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"", "nothing", "none", "null", "无", "暂无"}


def _distill_sync(*, current_memory: str, daily_notes: str) -> str:
    from app.llm import generate_completion

    if not settings.llm_api_key:
        return "NOTHING"

    prompt = DISTILLATION_PROMPT.format(
        current_memory=current_memory or "- 暂无",
        daily_notes=daily_notes,
    )
    messages = [
        {
            "role": "system",
            "content": "你是长期记忆蒸馏助手，只负责输出高质量 MEMORY.md 正文。",
        },
        {"role": "user", "content": prompt},
    ]
    try:
        return generate_completion(messages)
    except Exception as exc:
        logger.warning("dreaming distillation LLM call failed: %s", exc)
        return "NOTHING"


def run_dreaming(
    *,
    account_id: str,
    today: Optional[str] = None,
    days: int = 7,
) -> Dict[str, object]:
    """Distill recent daily notes into MEMORY.md for one account."""
    today = today or date.today().isoformat()
    days = max(1, min(days, 30))
    ensure_agent_context_files(account_id)

    records = list_recent_daily_memory(account_id=account_id, today=today, days=days)
    source_files = [
        {
            "date": str(record["date"]),
            "path": str(record["path"]),
            "chars": int(record["chars"]),
        }
        for record in records
    ]
    memory_path = context_file_path(account_id, "MEMORY.md")

    if not records:
        return {
            "status": "skipped",
            "reason": "no_daily_notes",
            "account_id": account_id,
            "today": today,
            "days": days,
            "source_files": source_files,
            "memory_path": str(memory_path),
            "memory_chars": len(read_long_term_memory(account_id)),
        }

    current_memory = read_long_term_memory(account_id)
    daily_notes = _format_daily_notes(records)
    try:
        distilled = _distill_sync(
            current_memory=current_memory,
            daily_notes=daily_notes,
        )
    except Exception as exc:
        logger.exception("dreaming failed account=%s error=%s", account_id, exc)
        return {
            "status": "skipped",
            "reason": "distillation_failed",
            "account_id": account_id,
            "today": today,
            "days": days,
            "source_files": source_files,
            "memory_path": str(memory_path),
            "memory_chars": len(current_memory),
        }

    next_memory = _normalize_memory_body(distilled)
    if _is_empty_distillation(next_memory):
        return {
            "status": "skipped",
            "reason": "nothing_to_promote",
            "account_id": account_id,
            "today": today,
            "days": days,
            "source_files": source_files,
            "memory_path": str(memory_path),
            "memory_chars": len(current_memory),
        }

    path = write_long_term_memory(account_id, next_memory)
    logger.info(
        "dreaming updated memory account=%s source_files=%s chars=%s",
        account_id,
        len(source_files),
        len(next_memory),
    )
    return {
        "status": "updated",
        "reason": None,
        "account_id": account_id,
        "today": today,
        "days": days,
        "source_files": source_files,
        "memory_path": str(path),
        "memory_chars": len(next_memory),
    }
