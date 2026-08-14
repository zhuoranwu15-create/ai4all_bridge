"""HTTP contracts for saving and publishing Plum Creation drafts."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError

from app.db import SessionPrincipal
from app.products.plum.api.contracts import (
    CreateCharacterRequest,
    CreateCreationDraftRequest,
    PublishCreationDraftRequest,
    UpdateCreationDraftRequest,
)
from app.products.plum.api.deps import require_plum_available, require_plum_member
from app.products.plum.application.character_creation import (
    CreateCharacterCommand,
    CharacterCreationConfirmationRequired,
    submit_creation_draft,
)
from app.products.plum.application.character_moderation import CharacterModerationUnavailable
from app.products.plum.infrastructure.creation_drafts import (
    archive_creation_draft,
    create_creation_draft,
    get_creation_draft,
    list_creation_drafts,
    update_creation_draft,
)
from app.products.plum.infrastructure.repository import PlumConflictError
from app.products.plum.infrastructure.tags import list_active_creator_tags

router = APIRouter(tags=["plum-creator-creation"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


@router.get("/creator/tags")
def creator_tags(
    response: Response,
    _available: None = Depends(require_plum_available),
) -> dict:
    """Return the active, platform-controlled vocabulary for Creation."""

    _no_store(response)
    return {"status": "ok", "items": list_active_creator_tags()}


@router.post("/creator/works")
def create_work_draft(
    payload: CreateCreationDraftRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Save a new incomplete draft without triggering review or publication."""

    try:
        draft = create_creation_draft(
            platform_user_id=principal.platform_user_id,
            content=payload.content.model_dump(),
        )
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", "work": draft}


@router.get("/creator/works")
def list_work_drafts(
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Return the creator's active My Studio items."""

    _no_store(response)
    return {
        "status": "ok",
        "items": list_creation_drafts(platform_user_id=principal.platform_user_id),
    }


@router.get("/creator/works/{work_id}")
def read_work_draft(
    work_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Restore one owner-scoped draft; non-owners receive the same 404."""

    draft = get_creation_draft(
        platform_user_id=principal.platform_user_id, work_id=work_id
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="creation_draft_not_found")
    _no_store(response)
    return {"status": "ok", "work": draft}


@router.patch("/creator/works/{work_id}")
def save_work_draft(
    work_id: str,
    payload: UpdateCreationDraftRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Save a complete snapshot against the latest known draft revision."""

    try:
        draft = update_creation_draft(
            platform_user_id=principal.platform_user_id,
            work_id=work_id,
            expected_revision=payload.expected_revision,
            content=payload.content.model_dump(),
        )
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except PlumConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", "work": draft}


@router.delete("/creator/works/{work_id}")
def delete_work_draft(
    work_id: str,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Archive one Studio item; non-owners receive the same 404."""

    if not archive_creation_draft(
        platform_user_id=principal.platform_user_id, work_id=work_id
    ):
        raise HTTPException(status_code=404, detail="creation_draft_not_found")
    _no_store(response)
    return {"status": "ok"}


@router.post("/creator/works/{work_id}/publish")
def publish_work_draft(
    work_id: str,
    payload: PublishCreationDraftRequest,
    response: Response,
    principal: SessionPrincipal = Depends(require_plum_member),
) -> dict:
    """Submit the exact saved revision and return its stable review status."""

    draft = get_creation_draft(
        platform_user_id=principal.platform_user_id, work_id=work_id
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="creation_draft_not_found")
    if int(draft["revision"]) != payload.expected_revision:
        raise HTTPException(status_code=409, detail="creation_draft_revision_conflict")
    try:
        validated = CreateCharacterRequest(
            **draft["content"], idempotency_key=payload.idempotency_key
        )
    except ValidationError as err:
        raise HTTPException(status_code=422, detail="creation_draft_incomplete") from err
    command = CreateCharacterCommand(
        idempotency_key=validated.idempotency_key,
        display_name=validated.display_name,
        gender=validated.gender,
        portrait_media_id=validated.portrait_media_id,
        portrait_position_x=validated.portrait_position_x,
        portrait_position_y=validated.portrait_position_y,
        portrait_zoom=validated.portrait_zoom,
        avatar_position_x=validated.avatar_position_x,
        avatar_position_y=validated.avatar_position_y,
        avatar_zoom=validated.avatar_zoom,
        intro=validated.intro,
        opening_scene=validated.opening_scene,
        character_settings=validated.character_settings,
        example_dialogues=validated.example_dialogues,
        response_rules=validated.response_rules,
        tag_ids=tuple(validated.tag_ids),
        creator_declared_rating=validated.creator_declared_rating,
        visibility=validated.visibility,
        adult_confirmed=validated.adult_confirmed,
        rights_confirmed=validated.rights_confirmed,
    )
    try:
        result = submit_creation_draft(
            platform_user_id=principal.platform_user_id,
            work_id=work_id,
            expected_revision=payload.expected_revision,
            command=command,
            published_character_id=str(draft.get("published_character_id") or ""),
        )
    except CharacterCreationConfirmationRequired as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except CharacterModerationUnavailable as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
    except PlumConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    _no_store(response)
    return {"status": "ok", **result}


__all__ = ["router"]
