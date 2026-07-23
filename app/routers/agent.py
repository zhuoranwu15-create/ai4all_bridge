"""Agent Runtime HTTP 路由。

端点：POST /agent/chat
鉴权：AI4ALL_BRIDGE_SECRET（与 Legacy bridge 共用，调用方持相同凭证）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import settings  # noqa: F401（conftest per-module patch 约定）
from app.agent_runtime.models import AgentChatRequest, AgentChatResponse
from app.agent_runtime.runner import run_agent
from app.routers.deps import verify_bridge_auth

logger = logging.getLogger("ai4all.routers.agent")

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    request: AgentChatRequest,
    _: None = Depends(verify_bridge_auth),
) -> AgentChatResponse:
    """Agent Runtime 入口。

    接受业务方（如 Nooki）的 Agent 请求，返回结构化意图理解和建议。
    与 /openclaw/turn（Legacy Chat）完全独立，不共享任何处理逻辑。
    """
    try:
        return run_agent(request)
    except ValueError as exc:
        # skill 不存在等已知错误
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("agent_chat unexpected error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent runtime error",
        ) from exc
