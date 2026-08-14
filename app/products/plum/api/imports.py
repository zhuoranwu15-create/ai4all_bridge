"""Reserved creator import boundaries for future backend parsing workflows."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.db import SessionPrincipal
from app.products.plum.api.deps import require_plum_member

router = APIRouter(tags=["plum-creator-imports"])


@router.post("/creator/imports/text", status_code=501)
async def reserve_text_import(
    file: UploadFile = File(...),
    _principal: SessionPrincipal = Depends(require_plum_member),
) -> None:
    """Reserve the upload contract without reading, parsing, or persisting data."""

    await file.close()
    raise HTTPException(
        status_code=501,
        detail="creator_text_import_not_implemented",
    )


__all__ = ["router"]
