"""Runtime LLM provider selection storage."""
from __future__ import annotations

from typing import Optional

from app.db._core import connect


_ACTIVE_PROVIDER_KEY = "active_provider_id"


def get_llm_provider_override_id() -> Optional[str]:
    """Return the runtime LLM provider override id, or None when unset."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM llm_runtime_config WHERE key = ?",
            (_ACTIVE_PROVIDER_KEY,),
        ).fetchone()
    if row is None:
        return None
    value = str(row["value"] or "").strip()
    return value or None


def set_llm_provider_override_id(
    provider_id: str,
    *,
    updated_by: Optional[str] = None,
) -> Optional[str]:
    """Persist a runtime LLM provider override id and return the saved value."""
    clean_provider_id = str(provider_id or "").strip()
    if not clean_provider_id:
        raise ValueError("provider_id is required")
    actor = str(updated_by or "").strip() or None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO llm_runtime_config(key, value, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (_ACTIVE_PROVIDER_KEY, clean_provider_id, actor),
        )
    return clean_provider_id


def clear_llm_provider_override_id() -> None:
    """Remove the runtime LLM provider override so settings default applies."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM llm_runtime_config WHERE key = ?",
            (_ACTIVE_PROVIDER_KEY,),
        )


def get_active_llm_provider_id() -> Optional[str]:
    """Backward-compatible alias for the runtime provider override id."""
    return get_llm_provider_override_id()


def set_active_llm_provider_id(
    provider_id: str,
    *,
    updated_by: Optional[str] = None,
) -> Optional[str]:
    """Backward-compatible alias for setting the runtime provider override id."""
    return set_llm_provider_override_id(provider_id, updated_by=updated_by)
