"""Compatibility HTTP route for the legacy Nooki skill agent."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.agent_runtime.legacy_chat import AgentChatRequest, AgentChatResponse, run_agent
from app.routers.deps import verify_bridge_auth


logger = logging.getLogger("ai4all.routers.agent")
router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    request: AgentChatRequest,
    _: None = Depends(verify_bridge_auth),
) -> AgentChatResponse:
    """Run one stateless skill-agent turn for legacy Nooki clients."""

    try:
        return run_agent(request)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("agent_chat unexpected error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent runtime error",
        ) from exc
