"""Owner-scoped persistence for incomplete Plum Creation drafts."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, Sequence

from app.db import connect
from app.platform.media.persistence import mark_media_assets_referenced
from app.products.plum.infrastructure.repository import PlumConflictError

_PREVIEW_PATH_PREFIX = "/api/v1/products/plum/creator/media"


def _decode(row) -> Dict[str, Any]:
    result = dict(row)
    result["content"] = json.loads(str(result.pop("content_json")))
    result["moderation_categories"] = json.loads(
        str(result.pop("moderation_categories_json") or "[]")
    )
    media_id = str(result.get("portrait_media_id") or "")
    result["portrait_preview_url"] = (
        f"{_PREVIEW_PATH_PREFIX}/{media_id}" if media_id else None
    )
    return result


def get_creation_draft(
    *, platform_user_id: str, work_id: str
) -> Optional[Dict[str, Any]]:
    """Return one draft only when it belongs to the requesting creator."""

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM plum_creation_drafts WHERE work_id=? AND owner_platform_user_id=?",
            (str(work_id).strip(), str(platform_user_id).strip()),
        ).fetchone()
    return _decode(row) if row is not None else None


def list_creation_drafts(*, platform_user_id: str) -> list[Dict[str, Any]]:
    """List active My Studio items newest first for one creator only."""

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM plum_creation_drafts
            WHERE owner_platform_user_id=? AND lifecycle_status='active'
            ORDER BY updated_at DESC, work_id DESC
            """,
            (str(platform_user_id).strip(),),
        ).fetchall()
    return [_decode(row) for row in rows]


def archive_creation_draft(*, platform_user_id: str, work_id: str) -> bool:
    """Soft-delete a Studio item and withdraw its published Character if present."""

    owner_id = str(platform_user_id).strip()
    cleaned_work_id = str(work_id).strip()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT published_character_id FROM plum_creation_drafts
            WHERE work_id=? AND owner_platform_user_id=? AND lifecycle_status='active'
            """,
            (cleaned_work_id, owner_id),
        ).fetchone()
        if row is None:
            return False
        character_id = str(row["published_character_id"] or "")
        if character_id:
            conn.execute(
                "UPDATE plum_characters SET status='archived', updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS') WHERE id=? AND work_id=?",
                (character_id, cleaned_work_id),
            )
            conn.execute(
                "UPDATE plum_works SET lifecycle_status='archived', updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS') WHERE id=? AND owner_platform_user_id=?",
                (cleaned_work_id, owner_id),
            )
        conn.execute(
            "UPDATE plum_creation_drafts SET lifecycle_status='archived', updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS') WHERE work_id=? AND owner_platform_user_id=?",
            (cleaned_work_id, owner_id),
        )
    return True


def create_creation_draft(
    *, platform_user_id: str, content: Dict[str, Any]
) -> Dict[str, Any]:
    """Create a new draft and retain its optional owner-scoped portrait."""

    owner_id = str(platform_user_id).strip()
    work_id = f"work_{uuid.uuid4().hex}"
    portrait_media_id = str(content.get("portrait_media_id") or "").strip()
    with connect() as conn:
        if portrait_media_id:
            try:
                mark_media_assets_referenced(
                    media_ids=[portrait_media_id],
                    owner_platform_user_id=owner_id,
                    conn=conn,
                )
            except ValueError as err:
                raise ValueError("creator_media_not_claimable") from err
        conn.execute(
            """
            INSERT INTO plum_creation_drafts(
                work_id, owner_platform_user_id, content_json, portrait_media_id
            ) VALUES (?, ?, ?, ?)
            """,
            (
                work_id,
                owner_id,
                json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                portrait_media_id or None,
            ),
        )
    result = get_creation_draft(platform_user_id=owner_id, work_id=work_id)
    if result is None:  # pragma: no cover - committed insert must be readable
        raise RuntimeError("creation_draft_missing_after_create")
    return result


def update_creation_draft(
    *, platform_user_id: str, work_id: str, expected_revision: int,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    """Replace a draft snapshot with optimistic concurrency protection."""

    owner_id = str(platform_user_id).strip()
    cleaned_work_id = str(work_id).strip()
    portrait_media_id = str(content.get("portrait_media_id") or "").strip()
    with connect() as conn:
        current = conn.execute(
            "SELECT revision, portrait_media_id FROM plum_creation_drafts WHERE work_id=? AND owner_platform_user_id=?",
            (cleaned_work_id, owner_id),
        ).fetchone()
        if current is None:
            raise LookupError("creation_draft_not_found")
        if int(current["revision"]) != int(expected_revision):
            raise PlumConflictError("creation_draft_revision_conflict")
        current_media_id = str(current["portrait_media_id"] or "")
        if portrait_media_id and portrait_media_id != current_media_id:
            try:
                mark_media_assets_referenced(
                    media_ids=[portrait_media_id],
                    owner_platform_user_id=owner_id,
                    conn=conn,
                )
            except ValueError as err:
                raise ValueError("creator_media_not_claimable") from err
        updated = conn.execute(
            """
            UPDATE plum_creation_drafts
            SET revision=revision+1, content_json=?, portrait_media_id=?,
                moderation_status='not_submitted',
                moderation_categories_json='[]',
                moderation_provider_reference=NULL,
                submitted_at=NULL, reviewed_at=NULL,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE work_id=? AND owner_platform_user_id=? AND revision=?
            """,
            (
                json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                portrait_media_id or None,
                cleaned_work_id,
                owner_id,
                int(expected_revision),
            ),
        )
        if updated.rowcount != 1:
            raise PlumConflictError("creation_draft_revision_conflict")
    result = get_creation_draft(platform_user_id=owner_id, work_id=cleaned_work_id)
    if result is None:  # pragma: no cover
        raise RuntimeError("creation_draft_missing_after_update")
    return result


def set_creation_draft_moderation(
    *, platform_user_id: str, work_id: str, expected_revision: int,
    status: str, categories: Sequence[str] = (), provider_reference: str = "",
    published_character_id: str = "",
) -> Dict[str, Any]:
    """Persist the review outcome for the exact submitted draft revision."""

    if status not in {"pending_review", "approved", "rejected"}:
        raise ValueError("creation_moderation_status_invalid")
    with connect() as conn:
        updated = conn.execute(
            """
            UPDATE plum_creation_drafts
            SET moderation_status=?, moderation_categories_json=?,
                moderation_provider_reference=NULLIF(?, ''),
                published_character_id=COALESCE(NULLIF(?, ''), published_character_id),
                submitted_at=COALESCE(submitted_at, to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')),
                reviewed_at=CASE WHEN ?='pending_review' THEN NULL ELSE to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS') END,
                updated_at=to_char((now() AT TIME ZONE 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
            WHERE work_id=? AND owner_platform_user_id=? AND revision=?
            """,
            (
                status,
                json.dumps(list(categories), ensure_ascii=False),
                str(provider_reference).strip(),
                str(published_character_id).strip(),
                status,
                str(work_id).strip(),
                str(platform_user_id).strip(),
                int(expected_revision),
            ),
        )
        if updated.rowcount != 1:
            raise PlumConflictError("creation_draft_revision_conflict")
    result = get_creation_draft(platform_user_id=platform_user_id, work_id=work_id)
    if result is None:  # pragma: no cover
        raise RuntimeError("creation_draft_missing_after_review")
    return result


__all__ = [
    "archive_creation_draft", "create_creation_draft", "get_creation_draft",
    "list_creation_drafts",
    "set_creation_draft_moderation", "update_creation_draft",
]
