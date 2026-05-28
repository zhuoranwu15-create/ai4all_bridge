import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.user_profiles import _safe_account_dir_name

logger = logging.getLogger("ai4all.memory_writer")


def memory_file_path(account_id: str, date_str: str) -> Path:
    """Return path to memory/YYYY-MM-DD.md for given account and date."""
    return (
        Path(settings.user_profiles_dir)
        / _safe_account_dir_name(account_id)
        / "memory"
        / f"{date_str}.md"
    )


def _visible_turns(turns: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """Return visible user/assistant turns with non-empty text."""
    visible: List[Tuple[str, str]] = []
    for turn in turns:
        role = turn.get("role", "")
        if role not in {"user", "assistant"}:
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            visible.append((role, content))
    return visible


def _single_line(value: Any) -> str:
    return " ".join(str(value).split())


def _format_metadata(metadata: Dict[str, Any]) -> str:
    ordered_keys = [
        "session_id",
        "user_message_id",
        "assistant_message_id",
        "sent_at",
        "modality",
        "source",
    ]
    keys = [key for key in ordered_keys if metadata.get(key) is not None]
    keys.extend(
        sorted(
            key
            for key in metadata
            if key not in ordered_keys and metadata.get(key) is not None
        )
    )
    lines = ["metadata:"]
    for key in keys:
        lines.append(f"- {key}: {_single_line(metadata[key])}")
    return "\n".join(lines)


def _format_daily_note_block(
    turns: List[Dict[str, str]],
    *,
    session_id: Optional[int] = None,
    user_message_id: Optional[str] = None,
    assistant_message_id: Optional[str] = None,
    sent_at: Optional[str] = None,
    modality: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Format raw visible conversation material for daily notes."""
    visible = _visible_turns(turns)
    if not visible:
        return ""

    timestamp = sent_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    turn_id = assistant_message_id or user_message_id or timestamp
    metadata: Dict[str, Any] = {
        "session_id": session_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "sent_at": timestamp,
        "modality": modality,
        "source": "messages",
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    parts = [f"## turn {_single_line(turn_id)}", "", _format_metadata(metadata), ""]
    for role, content in visible:
        label = "User" if role == "user" else "AI"
        parts.extend([f"{label}:", content, ""])
    return "\n".join(parts).rstrip() + "\n"


def _append_to_memory(path: Path, content: str, date_str: str) -> None:
    """Create or append to the memory file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {date_str}\n\n{content}", encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + content)


async def write_memory(
    account_id: str,
    turns: List[Dict[str, str]],
    today: str,
    *,
    session_id: Optional[int] = None,
    user_message_id: Optional[str] = None,
    assistant_message_id: Optional[str] = None,
    sent_at: Optional[str] = None,
    modality: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append raw visible turn text to the account daily notes file."""
    try:
        content = _format_daily_note_block(
            turns,
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            sent_at=sent_at,
            modality=modality,
            extra_metadata=extra_metadata,
        )
        if not content:
            return

        path = memory_file_path(account_id, today)
        await asyncio.to_thread(_append_to_memory, path, content, today)
        logger.debug("memory_writer: wrote %d chars to %s", len(content), path)

    except Exception as exc:
        logger.exception("memory_writer: unexpected error: %s", exc)
