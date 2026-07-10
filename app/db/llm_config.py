"""Runtime LLM selection storage (active family + per-tier provider overrides).

复用 KV 表 `llm_runtime_config`，三个 key：
- `active_family`      当前生效厂商家族（deepseek/openai/anthropic）
- `pro_provider_id`    pro 档的运行时 override provider id（可跨家族；空=用 active family 的 pro）
- `flash_provider_id`  flash 档的运行时 override provider id（同上）
"""
from __future__ import annotations

from typing import Dict, Optional

from app.db._core import connect


_ACTIVE_FAMILY_KEY = "active_family"
_TIER_OVERRIDE_KEYS = {"pro": "pro_provider_id", "flash": "flash_provider_id"}


def _get_value(conn, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM llm_runtime_config WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    value = str(row["value"] or "").strip()
    return value or None


def _set_value(conn, key: str, value: str, actor: Optional[str]) -> None:
    conn.execute(
        """
        INSERT INTO llm_runtime_config(key, value, updated_by)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_by = excluded.updated_by,
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        """,
        (key, value, actor),
    )


def get_llm_runtime_bindings() -> Dict[str, Optional[str]]:
    """Return runtime bindings: {active_family, pro_provider_id, flash_provider_id} (None when unset)."""
    with connect() as conn:
        active_family = _get_value(conn, _ACTIVE_FAMILY_KEY)
        pro_override = _get_value(conn, _TIER_OVERRIDE_KEYS["pro"])
        flash_override = _get_value(conn, _TIER_OVERRIDE_KEYS["flash"])
    return {
        "active_family": active_family,
        "pro_provider_id": pro_override,
        "flash_provider_id": flash_override,
    }


def set_active_family(family: str, *, updated_by: Optional[str] = None) -> str:
    """Persist the runtime active family. Also clears per-tier overrides so both tiers follow the family."""
    clean_family = str(family or "").strip()
    if not clean_family:
        raise ValueError("family is required")
    actor = str(updated_by or "").strip() or None
    with connect() as conn:
        _set_value(conn, _ACTIVE_FAMILY_KEY, clean_family, actor)
        for key in _TIER_OVERRIDE_KEYS.values():
            conn.execute("DELETE FROM llm_runtime_config WHERE key = ?", (key,))
    return clean_family


def set_tier_provider_override(
    tier: str,
    provider_id: str,
    *,
    updated_by: Optional[str] = None,
) -> str:
    """Override which provider serves a tier (may cross family). Returns the saved provider id."""
    clean_tier = str(tier or "").strip().lower()
    if clean_tier not in _TIER_OVERRIDE_KEYS:
        raise ValueError(f"unknown tier: {tier}")
    clean_provider_id = str(provider_id or "").strip()
    if not clean_provider_id:
        raise ValueError("provider_id is required")
    actor = str(updated_by or "").strip() or None
    with connect() as conn:
        _set_value(conn, _TIER_OVERRIDE_KEYS[clean_tier], clean_provider_id, actor)
    return clean_provider_id


def clear_tier_provider_override(tier: str) -> None:
    """Remove a tier's runtime override so it falls back to the active family's tier model."""
    clean_tier = str(tier or "").strip().lower()
    if clean_tier not in _TIER_OVERRIDE_KEYS:
        raise ValueError(f"unknown tier: {tier}")
    with connect() as conn:
        conn.execute(
            "DELETE FROM llm_runtime_config WHERE key = ?",
            (_TIER_OVERRIDE_KEYS[clean_tier],),
        )


def clear_llm_runtime_bindings() -> None:
    """Remove all runtime bindings (active family + both tier overrides), restoring settings defaults."""
    keys = [_ACTIVE_FAMILY_KEY, *_TIER_OVERRIDE_KEYS.values()]
    with connect() as conn:
        conn.executemany(
            "DELETE FROM llm_runtime_config WHERE key = ?",
            [(key,) for key in keys],
        )
