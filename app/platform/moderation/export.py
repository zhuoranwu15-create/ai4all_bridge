import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings
from app.db.admin import insert_admin_access_event
from app.platform.moderation.persistence import (
    create_content_moderation_export,
    get_content_moderation_export,
    get_content_moderation_task,
    insert_content_moderation_action,
    list_content_moderation_actions,
    list_content_moderation_results,
    update_content_moderation_task_review_status,
)


def _export_dir() -> Path:
    base = Path(str(getattr(settings, "moderation_export_dir", "data/moderation_exports") or "data/moderation_exports"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _assert_export_path(path: Path) -> Path:
    base = _export_dir().resolve()
    resolved = path.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("export artifact path escapes moderation export dir")
    return resolved


def create_export(
    *,
    task_id: str,
    admin_user: Dict[str, Any],
    reason: str,
    request_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a single-task moderation export artifact and audit the action."""

    task = get_content_moderation_task(task_id=task_id)
    if task is None:
        raise KeyError("moderation task not found")
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("reason is required")

    export_id = f"modexport_{uuid.uuid4().hex}"
    artifact = {
        "export_id": export_id,
        "task": task,
        "results": list_content_moderation_results(task_id=task_id),
        "actions": list_content_moderation_actions(task_id=task_id),
        "reason": clean_reason,
        "admin_user_id": str(admin_user.get("id") or ""),
    }
    artifact_path = _assert_export_path(_export_dir() / f"{export_id}.json")
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    export = create_content_moderation_export(
        export_id=export_id,
        task_id=task_id,
        account_id=str(task["account_id"]),
        admin_user_id=str(admin_user.get("id") or ""),
        reason=clean_reason,
        artifact_path=str(artifact_path),
        artifact={
            "format": "json",
            "task_id": task_id,
            "result_count": len(artifact["results"]),
            "action_count": len(artifact["actions"]),
        },
    )
    previous_status = str(task.get("status") or "")
    update_content_moderation_task_review_status(
        task_id=task_id,
        status="exported",
        reviewed_by_admin_user_id=str(admin_user.get("id") or ""),
    )
    insert_content_moderation_action(
        task_id=task_id,
        account_id=str(task["account_id"]),
        admin_user_id=str(admin_user.get("id") or ""),
        action="export",
        previous_status=previous_status,
        next_status="exported",
        reason=clean_reason,
        metadata={"export_id": export_id},
    )
    insert_admin_access_event(
        admin_user_id=str(admin_user.get("id") or ""),
        action="moderation.export",
        resource_type="moderation_export",
        resource_id=export_id,
        account_id=str(task["account_id"]),
        plaintext=True,
        reason=clean_reason,
        request_path=request_path or f"/admin/moderation/tasks/{task_id}/export",
        metadata={"task_id": task_id},
    )
    return {"export": export, "artifact": artifact}


def load_export_artifact(*, export_id: str) -> Dict[str, Any]:
    """Load an existing moderation export artifact from disk."""

    export = get_content_moderation_export(export_id=export_id)
    if export is None:
        raise KeyError("moderation export not found")
    artifact_path = export.get("artifact_path")
    if not artifact_path:
        raise FileNotFoundError("moderation export artifact path missing")
    path = _assert_export_path(Path(str(artifact_path)))
    return json.loads(path.read_text(encoding="utf-8"))
