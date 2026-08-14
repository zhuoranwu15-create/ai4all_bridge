"""File-backed operating source and PostgreSQL projection for Plum Tags."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Tuple

from app.db import connect


DEFAULT_TAG_CONFIG_PATH = Path(__file__).resolve().parents[4] / "data/plum/tags.json"
_TAG_ID_PATTERN = re.compile(r"^tag_[a-z0-9]+(?:_[a-z0-9]+)*$")
_TAG_CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_TAG_STATUSES = {"active", "disabled"}


class TagConfigError(ValueError):
    """The offline Tag configuration is unsafe to synchronize."""


@dataclass(frozen=True)
class TagDefinition:
    """One stable, platform-controlled Plum Tag."""

    id: str
    code: str
    display_name: str
    status: str
    sort_order: int


def _required_text(item: dict, field: str, *, max_length: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TagConfigError(f"tag_{field}_required")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise TagConfigError(f"tag_{field}_too_long")
    return cleaned


def load_tag_config(path: Path | str = DEFAULT_TAG_CONFIG_PATH) -> Tuple[TagDefinition, ...]:
    """Load and strictly validate the versioned offline Tag source."""

    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise TagConfigError("tag_config_unreadable") from err
    if (
        not isinstance(payload, dict)
        or isinstance(payload.get("version"), bool)
        or payload.get("version") != 1
    ):
        raise TagConfigError("tag_config_version_unsupported")
    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        raise TagConfigError("tag_config_tags_required")

    tags = []
    seen_ids = set()
    seen_codes = set()
    seen_names = set()
    for raw in raw_tags:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "code",
            "display_name",
            "status",
            "sort_order",
        }:
            raise TagConfigError("tag_config_fields_invalid")
        tag_id = _required_text(raw, "id", max_length=80)
        code = _required_text(raw, "code", max_length=80)
        display_name = _required_text(raw, "display_name", max_length=80)
        status = _required_text(raw, "status", max_length=20)
        sort_order = raw.get("sort_order")
        if not _TAG_ID_PATTERN.fullmatch(tag_id):
            raise TagConfigError("tag_id_invalid")
        if not _TAG_CODE_PATTERN.fullmatch(code):
            raise TagConfigError("tag_code_invalid")
        if status not in _TAG_STATUSES:
            raise TagConfigError("tag_status_invalid")
        if (
            isinstance(sort_order, bool)
            or not isinstance(sort_order, int)
            or not 0 <= sort_order <= 1_000_000
        ):
            raise TagConfigError("tag_sort_order_invalid")
        if tag_id in seen_ids:
            raise TagConfigError("tag_id_duplicate")
        if code in seen_codes:
            raise TagConfigError("tag_code_duplicate")
        normalized_name = display_name.casefold()
        if normalized_name in seen_names:
            raise TagConfigError("tag_display_name_duplicate")
        seen_ids.add(tag_id)
        seen_codes.add(code)
        seen_names.add(normalized_name)
        tags.append(TagDefinition(tag_id, code, display_name, status, sort_order))
    return tuple(tags)


def sync_tag_config(
    path: Path | str = DEFAULT_TAG_CONFIG_PATH,
) -> Dict[str, int]:
    """Upsert configured Tags atomically without disabling omitted rows."""

    tags = load_tag_config(path)
    inserted = updated = unchanged = 0
    with connect() as conn:
        for tag in tags:
            rows = conn.execute(
                """
                SELECT id, code, display_name, status, sort_order
                FROM plum_tags WHERE id=? OR code=?
                """,
                (tag.id, tag.code),
            ).fetchall()
            by_id = next((row for row in rows if str(row["id"]) == tag.id), None)
            wrong_id = next((row for row in rows if str(row["id"]) != tag.id), None)
            if wrong_id is not None:
                raise TagConfigError("tag_code_identity_conflict")
            if by_id is not None and str(by_id["code"]) != tag.code:
                raise TagConfigError("tag_id_identity_conflict")
            if by_id is None:
                conn.execute(
                    """
                    INSERT INTO plum_tags(id, code, display_name, status, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (tag.id, tag.code, tag.display_name, tag.status, tag.sort_order),
                )
                inserted += 1
                continue
            current = (
                str(by_id["display_name"]),
                str(by_id["status"]),
                int(by_id["sort_order"]),
            )
            desired = (tag.display_name, tag.status, tag.sort_order)
            if current == desired:
                unchanged += 1
                continue
            conn.execute(
                """
                UPDATE plum_tags
                SET display_name=?, status=?, sort_order=?,
                    updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
                WHERE id=?
                """,
                (tag.display_name, tag.status, tag.sort_order, tag.id),
            )
            updated += 1
    return {
        "configured": len(tags),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
    }


def list_active_creator_tags() -> list[dict]:
    """Return the stable active Tag vocabulary in operating order."""

    with connect() as conn:
        rows: Iterable = conn.execute(
            """
            SELECT id, code, display_name, sort_order
            FROM plum_tags
            WHERE status='active'
            ORDER BY sort_order, id
            """
        ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "code": str(row["code"]),
            "display_name": str(row["display_name"]),
            "sort_order": int(row["sort_order"]),
        }
        for row in rows
    ]


__all__ = [
    "DEFAULT_TAG_CONFIG_PATH",
    "TagConfigError",
    "TagDefinition",
    "list_active_creator_tags",
    "load_tag_config",
    "sync_tag_config",
]
