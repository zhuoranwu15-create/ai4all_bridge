"""Companion World P1 控制面：home bootstrap/candidates/confirm/residents。"""
from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.db import get_platform_user_by_session_token
from app.domains.companion_world import (
    CandidateRecord,
    CompanionWorldError,
    CompanionWorldService,
    ResidentRecord,
    ResidentSelection,
    TemplateDraft,
)
from app.platform import SqlCompanionWorldRepository
from app.time_utils import beijing_now

router = APIRouter(prefix="/v1", tags=["companion-world"])

_ERROR_STATUS = {
    "not_found": 404,
    "unauthorized": 401,
    "account_id_not_accepted": 400,
    "world_not_ready": 409,
    "world_disabled": 403,
    "template_not_found": 404,
    "template_not_available": 409,
    "preset_catalog_not_ready": 503,
    "resident_not_found": 404,
    "resident_capacity_exceeded": 409,
    "resident_capacity_empty": 409,
    "resident_selection_invalid": 400,
    "resident_already_exists": 409,
    "custom_candidate_limit_exceeded": 409,
    "conversation_not_found": 404,
}


class CompanionWorldApiError(Exception):
    """仅供新世界端点使用的稳定 HTTP 错误。"""

    def __init__(self, code: str, status_code: Optional[int] = None) -> None:
        self.code = code
        self.status_code = status_code or _ERROR_STATUS.get(code, 500)
        super().__init__(code)


class SelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=50)


class ConfirmResidentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[SelectionPayload] = Field(max_length=10)


class CreateResidentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    persona_hint: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CreateResidentPayload":
        if bool(self.template_id) == bool(self.name):
            raise ValueError("provide exactly one of template_id or name")
        if self.template_id and self.persona_hint:
            raise ValueError("persona_hint requires name")
        return self


def _request_id(request: Request) -> str:
    current = getattr(request.state, "companion_world_request_id", None)
    if current:
        return str(current)
    value = f"req_{uuid.uuid4().hex}"
    request.state.companion_world_request_id = value
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


def _require_world_session(
    authorization: Optional[str] = Header(default=None),
) -> dict:
    if not bool(getattr(settings, "companion_world_p1_enabled", False)):
        raise CompanionWorldApiError("not_found", 404)
    if not authorization or not authorization.startswith("Bearer "):
        raise CompanionWorldApiError("unauthorized", 401)
    token = authorization.removeprefix("Bearer ").strip()
    platform_user = (
        get_platform_user_by_session_token(token=token) if token else None
    )
    if platform_user is None:
        raise CompanionWorldApiError("unauthorized", 401)
    return platform_user


def _service() -> CompanionWorldService:
    return CompanionWorldService(SqlCompanionWorldRepository())


def _run_domain(action: Callable):
    try:
        return action()
    except CompanionWorldError as err:
        raise CompanionWorldApiError(err.code) from err


def _candidate_data(candidate: CandidateRecord) -> dict:
    """序列化候选公开字段；刻意不含 persona_seed_json 和内部 resident id。"""
    return {
        "template_id": candidate.template.id,
        "template_version": candidate.template_version,
        "name": candidate.template.name,
        "avatar_ref": candidate.template.avatar_ref,
        "summary": candidate.template.summary,
        "tags": list(candidate.template.tags),
        "origin": candidate.origin,
        "status": candidate.status,
    }


def _resident_data(resident: ResidentRecord) -> dict:
    """序列化 owner 可见居民字段；不暴露 runtime account id。"""
    return {
        "resident_id": resident.resident_id,
        "name": resident.name,
        "avatar_ref": resident.avatar_ref,
        "status": resident.status,
        "origin": resident.origin,
        "conversation_id": resident.conversation_id,
        "conversation_state": resident.conversation_state,
    }


@router.post("/worlds/home/bootstrap")
def bootstrap_home(
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_world_session),
) -> dict:
    result = _run_domain(lambda: _service().bootstrap_home(platform_user["id"]))
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
        },
    )


@router.get("/worlds/home/resident-candidates")
def list_candidates(
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_world_session),
) -> dict:
    candidates = _run_domain(lambda: _service().list_candidates(platform_user["id"]))
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"candidates": [_candidate_data(item) for item in candidates]},
    )


@router.post("/worlds/home/residents/confirm")
def confirm_residents(
    payload: ConfirmResidentsPayload,
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_world_session),
) -> dict:
    residents = _run_domain(
        lambda: _service().confirm_residents(
            platform_user["id"],
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


@router.get("/worlds/home/residents")
def list_residents(
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_world_session),
) -> dict:
    residents = _run_domain(lambda: _service().list_residents(platform_user["id"]))
    _no_store(response)
    return _envelope(
        request,
        code="ok",
        data={"residents": [_resident_data(item) for item in residents]},
    )


@router.post("/worlds/home/residents")
def create_resident(
    payload: CreateResidentPayload,
    request: Request,
    response: Response,
    platform_user: dict = Depends(_require_world_session),
) -> dict:
    custom = None
    if payload.name:
        name = payload.name.strip()
        hint = (payload.persona_hint or "").strip()
        soul = hint or f"你是{name}，是用户世界里一位独立、自然的 AI 陪伴。"
        custom = TemplateDraft(
            name=name,
            persona_seed_json=json.dumps(
                {
                    "SOUL.md": f"# SOUL\n\n{soul}",
                    "IDENTITY.md": (
                        f"# IDENTITY\n\n- 你的名字是 {name}，用它自称。\n"
                        "- 你是用户的个人 AI 陪伴。"
                    ),
                },
                ensure_ascii=False,
            ),
        )
    result = _run_domain(
        lambda: _service().create_resident(
            platform_user["id"],
            template_id=payload.template_id,
            custom_template=custom,
        )
    )
    _no_store(response)
    if isinstance(result, CandidateRecord):
        data = {"candidate": _candidate_data(result)}
    else:
        data = {"resident": _resident_data(result)}
    return _envelope(request, code="ok", data=data)


async def _api_error_handler(request: Request, exc: CompanionWorldApiError):
    response = JSONResponse(
        status_code=exc.status_code,
        content=_envelope(request, code=exc.code),
    )
    _no_store(response)
    return response


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/worlds/"):
        forbidden_account_id = isinstance(exc.body, dict) and bool(
            {"account_id", "runtime_account_id"}.intersection(exc.body)
        )
        code = "account_id_not_accepted" if forbidden_account_id else "invalid_request"
        response = JSONResponse(
            status_code=400 if forbidden_account_id else 422,
            content=_envelope(request, code=code),
        )
        _no_store(response)
        return response
    return await request_validation_exception_handler(request, exc)


def install_exception_handlers(app) -> None:
    """在 composition root 注册仅影响新世界端点的稳定错误信封。"""
    app.add_exception_handler(CompanionWorldApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
