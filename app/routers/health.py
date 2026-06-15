"""健康检查路由：/health, /health/live, /health/ready。"""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import connect as db_connect

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@router.get("/health/live")
def health_live() -> dict:
    return {"status": "ok", "env": settings.app_env}


def _check_writable_dir(path_value: str) -> dict:
    path = Path(path_value)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ai4all_ready_check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"status": "ok", "path": str(path)}
    except Exception as err:
        return {"status": "error", "path": str(path), "error": str(err)}


def _check_runtime_config() -> dict:
    env = str(getattr(settings, "app_env", "") or "").lower()
    if env in {"local", "development", "test"}:
        return {"status": "ok", "mode": "local"}
    missing = []
    if not getattr(settings, "llm_api_key", ""):
        missing.append("LLM_API_KEY")
    if not getattr(settings, "ai4all_bridge_secret", "") or settings.ai4all_bridge_secret == "dev-secret":
        missing.append("AI4ALL_BRIDGE_SECRET")
    if not getattr(settings, "admin_token", "") or settings.admin_token == "dev-admin-token":
        missing.append("ADMIN_TOKEN")
    if missing:
        return {"status": "error", "missing": missing}
    return {"status": "ok", "mode": env or "production"}


def _build_ready_status() -> tuple[int, dict]:
    checks: dict[str, dict] = {}
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = {"status": "ok"}
    except Exception as err:
        checks["db"] = {"status": "error", "error": str(err)}

    checks["user_profiles_dir"] = _check_writable_dir(settings.user_profiles_dir)
    checks["system_dir"] = _check_writable_dir(settings.system_dir)
    checks["runtime_config"] = _check_runtime_config()

    ok = all(item.get("status") == "ok" for item in checks.values())
    return (
        200 if ok else 503,
        {
            "status": "ok" if ok else "error",
            "env": settings.app_env,
            "checks": checks,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


@router.get("/health/ready")
def health_ready() -> JSONResponse:
    status_code, payload = _build_ready_status()
    return JSONResponse(status_code=status_code, content=payload)
