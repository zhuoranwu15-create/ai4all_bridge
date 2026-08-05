"""鸣蝉 World bootstrap、候选确认与居民列表 API。"""
from __future__ import annotations

import uuid
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.bootstrap.product_registry import (
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.config import settings
from app.db import SessionPrincipal
from app.products.mingchan.api.contracts import (
    MingchanBootstrapResponse,
    MingchanCandidateListResponse,
    MingchanConfirmResidentsRequest,
    MingchanResidentListResponse,
)
from app.products.mingchan.api.deps import (
    build_mingchan_enabled_dependency,
    build_mingchan_session_dependency,
)
from app.products.mingchan.domain.companion_world import (
    CandidateRecord,
    MingchanWorldError,
    MingchanWorldOnboardingService,
    ResidentRecord,
    ResidentSelection,
)
from app.products.mingchan.infrastructure.world_onboarding import (
    SqlMingchanWorldOnboardingRepository,
)
from app.time_utils import beijing_now

_ERROR_STATUS = {
    "feature_disabled": 404,
    "unauthorized": 401,
    "world_not_ready": 409,
    "world_disabled": 403,
    "legacy_world_cleanup_required": 409,
    "preset_catalog_not_ready": 503,
    "resident_not_found": 404,
    "resident_capacity_exceeded": 409,
    "resident_capacity_empty": 409,
    "resident_selection_invalid": 400,
    "display_name_invalid": 400,
    "resident_product_mismatch": 409,
    "conversation_not_found": 404,
    "notification_not_found": 404,
    "invalid_cursor": 400,
    "invalid_request": 400,
    "quiet_level_invalid": 422,
    "conversation_read_only": 409,
    "turn_in_progress": 409,
    "rate_limited": 429,
    "account_disabled": 403,
    "message_content_required": 422,
    "media_content_required": 422,
    "media_ref_invalid": 400,
    "media_ref_expired": 409,
    "media_disabled": 409,
}


class MingchanWorldApiError(Exception):
    """鸣蝉 World API 的稳定错误码。"""

    def __init__(self, code: str, status_code: Optional[int] = None) -> None:
        self.code = code
        self.status_code = status_code or _ERROR_STATUS.get(code, 500)
        super().__init__(code)


def _request_id(request: Request) -> str:
    current = getattr(request.state, "mingchan_world_request_id", None)
    if current:
        return str(current)
    value = f"req_{uuid.uuid4().hex}"
    request.state.mingchan_world_request_id = value
    return value


def _envelope(request: Request, *, code: str, data=None) -> dict:
    payload = {
        "code": code,
        "request_id": _request_id(request),
        "server_time": beijing_now().isoformat(timespec="seconds"),
    }
    if code == "ok":
        payload["data"] = data
    else:
        payload["message"] = None
    return payload


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _candidate_data(candidate: CandidateRecord) -> dict:
    suggested = (candidate.suggested_display_name or "").strip()
    return {
        "template_id": candidate.template.id,
        "template_version": candidate.template_version,
        "name": candidate.template.name,
        "avatar_ref": candidate.template.avatar_ref,
        "summary": candidate.template.summary,
        "long_summary": candidate.template.long_summary,
        "tags": list(candidate.template.tags),
        "origin": candidate.origin,
        "status": candidate.status,
        "persona_key": candidate.template.persona_key,
        "suggested_display_name": candidate.suggested_display_name,
        "naming_version": candidate.naming_version,
        "naming_status": "ready" if suggested else "unavailable",
    }


def _resident_data(resident: ResidentRecord) -> dict:
    return {
        "resident_id": resident.resident_id,
        "name": resident.name,
        "avatar_ref": resident.avatar_ref,
        "status": resident.status,
        "origin": resident.origin,
        "conversation_id": resident.conversation_id,
        "conversation_state": resident.conversation_state,
        "mission_display": (
            "narrative" if resident.persona_key == "sichen" else "countable"
        ),
    }


def _run_domain(action: Callable):
    try:
        return action()
    except MingchanWorldError as err:
        raise MingchanWorldApiError(err.code) from err


async def mingchan_world_error_handler(
    request: Request,
    error: MingchanWorldApiError,
) -> JSONResponse:
    """把鸣蝉 World 错误投影成稳定、无内部文案的信封。"""

    response = JSONResponse(
        status_code=error.status_code,
        content=_envelope(request, code=error.code),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def install_exception_handlers(app) -> None:
    """安装鸣蝉 World 专属异常处理器，不覆盖朝夕或共享 HTTP 异常。"""

    app.add_exception_handler(MingchanWorldApiError, mingchan_world_error_handler)


def build_router(
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    *,
    config=None,
) -> APIRouter:
    """创建固定鸣蝉 audience 的 World onboarding router。"""

    world_config = settings if config is None else config
    require_enabled = build_mingchan_enabled_dependency(registry)
    require_session = build_mingchan_session_dependency(registry)

    def require_world_session(
        authorization: Optional[str] = Header(default=None),
    ) -> SessionPrincipal:
        if not bool(getattr(world_config, "mingchan_p1_enabled", False)):
            raise MingchanWorldApiError("feature_disabled")
        try:
            return require_session(authorization=authorization)
        except HTTPException as err:
            raise MingchanWorldApiError("unauthorized", 401) from err

    def service() -> MingchanWorldOnboardingService:
        return MingchanWorldOnboardingService(
            SqlMingchanWorldOnboardingRepository(registry=registry)
        )

    router = APIRouter(
        tags=["mingchan-world-onboarding"],
        dependencies=[Depends(require_enabled)],
    )

    @router.post(
        "/worlds/home/bootstrap",
        response_model=MingchanBootstrapResponse,
    )
    def bootstrap_home(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_world_session),
    ) -> dict:
        result = _run_domain(
            lambda: service().bootstrap_home(principal.platform_user_id)
        )
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={
                "world": {
                    "id": result.world.id,
                    "status": result.world.status,
                    "onboarding_state": result.world.onboarding_state,
                },
                "candidates": [_candidate_data(item) for item in result.candidates],
                "existing_residents": [
                    _resident_data(item) for item in result.residents
                ],
            },
        )

    @router.get(
        "/worlds/home/resident-candidates",
        response_model=MingchanCandidateListResponse,
    )
    def list_candidates(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_world_session),
    ) -> dict:
        candidates = _run_domain(
            lambda: service().list_candidates(principal.platform_user_id)
        )
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={"candidates": [_candidate_data(item) for item in candidates]},
        )

    @router.post(
        "/worlds/home/residents/confirm",
        response_model=MingchanResidentListResponse,
    )
    def confirm_residents(
        payload: MingchanConfirmResidentsRequest,
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_world_session),
    ) -> dict:
        residents = _run_domain(
            lambda: service().confirm_residents(
                principal.platform_user_id,
                [
                    ResidentSelection(
                        template_id=item.template_id,
                        display_name=item.display_name,
                    )
                    for item in payload.selections
                ],
            )
        )
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={"residents": [_resident_data(item) for item in residents]},
        )

    @router.get(
        "/worlds/home/residents",
        response_model=MingchanResidentListResponse,
    )
    def list_residents(
        request: Request,
        response: Response,
        principal: SessionPrincipal = Depends(require_world_session),
    ) -> dict:
        residents = _run_domain(
            lambda: service().list_residents(principal.platform_user_id)
        )
        _no_store(response)
        return _envelope(
            request,
            code="ok",
            data={"residents": [_resident_data(item) for item in residents]},
        )

    return router


__all__ = [
    "MingchanWorldApiError",
    "build_router",
    "install_exception_handlers",
    "mingchan_world_error_handler",
]
