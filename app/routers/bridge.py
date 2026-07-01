"""OpenClaw bridge / 多机节点路由（Bearer bridge_secret）。

从 app.main 拆出（结构优化，函数体逐字保留）。后台事件循环改为请求时从
app.app_runtime.get_background_loop() 读取。
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends

from app.config import settings
from app.app_runtime import get_background_loop
from app.identity import identity_response_metadata, resolve_openclaw_identity
from app.turn_service import handle_openclaw_turn
from app.schemas import (
    NodeHeartbeatRequest,
    NodeOutboundClaimRequest,
    NodeOutboundResultRequest,
    OpenClawDebugTraceRequest,
    OpenClawTurnRequest,
    OpenClawTurnResponse,
)
from app.routers.deps import verify_bridge_auth
from app.db import (
    claim_pending_outbound_by_node,
    get_content_invitation,
    get_or_create_session,
    insert_debug_trace,
    insert_outbound_delivery_message,
    mark_content_invitation_invited,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
    release_content_invitation_claim,
    resolve_account_id_for_inbound_channel_identity,
    upsert_access_node,
    upsert_channel_binding,
)
from app.proactive.store.candidates import (
    REACTIVATION_TYPE_CONTENT_INVITATION,
    clear_reactivation_candidate,
)
from app.proactive.contract.common import format_reactivation_time
from app.time_utils import beijing_naive_now

logger = logging.getLogger("ai4all")
router = APIRouter()


def _complete_reactivation_content_invitation_outbound(
    *,
    outbound_message: Optional[dict],
    final_status: str,
) -> None:
    """Advance content-invitation state after a remote node reports final delivery."""
    if not outbound_message:
        return
    metadata = outbound_message.get("metadata") or {}
    if not metadata.get("reactivation"):
        return
    if metadata.get("reactivation_type") != REACTIVATION_TYPE_CONTENT_INVITATION:
        return

    invitation_id = str(metadata.get("content_invitation_id") or "").strip()
    if not invitation_id:
        return
    account_id = str(outbound_message.get("account_id") or "").strip()
    invitation = get_content_invitation(invitation_id=invitation_id)
    if invitation is None or invitation.get("account_id") != account_id:
        logger.warning(
            "content invitation finalization account mismatch outbound_id=%s invitation_id=%s",
            outbound_message.get("id"),
            invitation_id,
        )
        return

    if final_status == "sent":
        current = beijing_naive_now()
        invited_at = str(outbound_message.get("sent_at") or "").strip()
        if not invited_at:
            invited_at = format_reactivation_time(current)
        outbound_id = (
            int(outbound_message["id"])
            if outbound_message.get("id") is not None
            else None
        )
        updated = mark_content_invitation_invited(
            invitation_id=invitation_id,
            outbound_message_id=outbound_id,
            invited_at=invited_at,
        )
        if updated is None:
            logger.warning(
                "content invitation finalization skipped outbound_id=%s invitation_id=%s",
                outbound_message.get("id"),
                invitation_id,
            )
        if account_id:
            clear_reactivation_candidate(
                account_id=account_id,
                reason="sent",
                now=current,
            )
        return

    if final_status == "failed":
        release_content_invitation_claim(invitation_id=invitation_id)


@router.post("/openclaw/debug-traces")
def openclaw_debug_trace_ingest(
    payload: OpenClawDebugTraceRequest,
    _: None = Depends(verify_bridge_auth),
) -> dict:
    channel_account_id = payload.channel_account_id or payload.account_id
    fallback_session_key = (
        payload.session_key
        or (f"openclaw-debug:{channel_account_id}" if channel_account_id else "openclaw-debug:unknown")
    )
    identity = resolve_openclaw_identity(
        channel=payload.channel or "openclaw",
        session_key=fallback_session_key,
        channel_account_id=channel_account_id,
        sender_id=None,
        chat_id=None,
    )
    # debug-traces 是显式调试入口，保留 session_key 兜底（resolve 现在无绑定时返回 None）。
    account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    ) or identity.session_key
    session_state = get_or_create_session(
        account_id=account_id,
        channel=identity.channel,
        sender_id=identity.sender_id,
        sender_name=None,
        chat_id=identity.chat_id,
        session_key=identity.session_key,
    )
    binding = upsert_channel_binding(
        account_id=account_id,
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
        sender_id=identity.sender_id,
        chat_id=identity.chat_id,
        raw_identity=identity_response_metadata(identity, account_id),
    )
    session = session_state["session"]
    trace_id = payload.trace_id or f"trace-{uuid.uuid4()}"
    metadata = dict(payload.metadata or {})
    metadata["identity"] = identity_response_metadata(identity, account_id)
    metadata["channel_binding_id"] = binding["id"]
    inserted_id = insert_debug_trace(
        trace_id=trace_id,
        account_id=account_id,
        session_id=session["id"],
        message_id=payload.message_id,
        source=payload.source or "openclaw",
        llm_model=payload.llm_model,
        system_prompt=payload.system_prompt,
        messages=payload.messages,
        reply=payload.reply,
        metadata=metadata,
        latency_ms=payload.latency_ms,
        error=payload.error,
    )
    return {
        "status": "ok" if inserted_id is not None else "duplicate",
        "trace_id": trace_id,
        "metadata": identity_response_metadata(identity, account_id),
    }


@router.post("/openclaw/turn", response_model=OpenClawTurnResponse)
def openclaw_turn(
    payload: OpenClawTurnRequest,
    _: None = Depends(verify_bridge_auth),
) -> OpenClawTurnResponse:
    return handle_openclaw_turn(payload, background_loop=get_background_loop())


# ===== 多机接入:节点面向 API(Bearer bridge_secret;见 multi_node_access_refactor.md §7.1)=====


@router.post("/node/outbound/claim")
def node_outbound_claim(
    payload: NodeOutboundClaimRequest,
    _: None = Depends(verify_bridge_auth),
) -> dict:
    """节点认领其归属的待发主动消息。claim/标记都在中心(节点无 DB)。"""
    messages = claim_pending_outbound_by_node(
        node_id=payload.node_id,
        batch_size=payload.batch,
        claim_timeout_seconds=settings.outbound_claim_timeout_seconds,
    )
    return {"messages": messages}


@router.post("/node/outbound/{outbound_message_id}/result")
def node_outbound_result(
    outbound_message_id: int,
    payload: NodeOutboundResultRequest,
    _: None = Depends(verify_bridge_auth),
) -> dict:
    """节点回报发送结果。sent → 标记已发 + 落交付记录;否则标记失败(stale 回收/下轮重领)。"""
    if payload.status == "sent":
        sent = mark_outbound_message_sent(
            outbound_message_id=outbound_message_id,
            gateway_message_id=payload.gateway_message_id,
        )
        if sent is not None:
            try:
                insert_outbound_delivery_message(outbound_message=sent)
            except Exception as err:  # 落交付记录失败不应翻车发送结果
                logger.exception(
                    "node_outbound_result delivery insert failed id=%s error=%s",
                    outbound_message_id,
                    err,
                )
            _complete_reactivation_content_invitation_outbound(
                outbound_message=sent,
                final_status="sent",
            )
        return {"status": "sent", "outbound_message": sent}
    failed = mark_outbound_message_failed(
        outbound_message_id=outbound_message_id,
        error=str(payload.error or "node_send_failed"),
    )
    _complete_reactivation_content_invitation_outbound(
        outbound_message=failed,
        final_status="failed",
    )
    return {"status": "failed", "outbound_message": failed}


@router.post("/node/heartbeat")
def node_heartbeat(
    payload: NodeHeartbeatRequest,
    _: None = Depends(verify_bridge_auth),
) -> dict:
    """节点心跳:upsert access_nodes(仅覆盖传入的非 None 字段)。"""
    node = upsert_access_node(
        node_id=payload.node_id,
        base_url=payload.base_url,
        egress_ip=payload.egress_ip,
        session_count=payload.session_count,
        max_sessions=payload.max_sessions,
    )
    return {"node": node}
