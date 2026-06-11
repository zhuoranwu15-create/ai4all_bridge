from typing import Any, Dict, Optional

from app.config import settings
from app.moderation.models import MachineReviewResult


def review_image_task(task: Dict[str, Any]) -> Optional[MachineReviewResult]:
    """Run optional image safety review for an image moderation task.

    Phase B only wires the boundary. The concrete provider/model is still an
    open product decision, so enabling the switch without model config returns a
    machine error that the worker can retry and then send to human review.
    """

    if str(task.get("content_kind") or "") != "image":
        return None
    if not bool(getattr(settings, "moderation_image_safety_enabled", False)):
        return None
    model = str(getattr(settings, "moderation_image_safety_model", "") or "").strip()
    if not model:
        return MachineReviewResult(
            reviewer_type="image_safety",
            engine="unconfigured_image_safety",
            engine_version="",
            level="error",
            reason="image safety is enabled but no model/provider is configured",
            error="moderation_image_safety_not_configured",
            raw_result={"media": task.get("media") or {}},
        )
    return MachineReviewResult(
        reviewer_type="image_safety",
        engine="unimplemented_image_safety",
        engine_version=model,
        level="error",
        reason="image safety provider integration is not implemented",
        error="moderation_image_safety_unimplemented",
        raw_result={"media": task.get("media") or {}},
    )

