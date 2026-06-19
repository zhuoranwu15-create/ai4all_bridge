"""瘦接入节点 agent(node-only 远程机,如 aliyun2)。

职责三件,**全程不碰 SQLite**(设计 §7.2):
- **登录 exec 端点**:被中心 `node_gateway` push,内部调本机 `openclaw_gateway` 跑 openclaw。
- **出站 pull 循环**:轮询中心 `/node/outbound/claim` 领自己归属的待发主动消息,本机发送后
  回报 `/node/outbound/{id}/result`(claim/标记都在中心,设计附录 B)。
- **心跳**:周期 upsert 中心 `access_nodes`。

中心(aliyun1 同机 central+node)不需要本模块的 exec 端点:`node_gateway._is_local_node`
命中本机即直调,不走 HTTP。本 app 只在 node-only 机由 `scripts/run_access_node.py` 起。
"""
import logging
import threading
from typing import Any, Dict, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from app import openclaw_gateway
from app.config import settings
from app.openclaw_gateway import DEFAULT_WEIXIN_CHANNEL, OpenClawGatewayError

logger = logging.getLogger("ai4all.node_agent")


# ----------------------------- exec 端点(被中心 push) -----------------------------

def _verify_bridge_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """节点 exec 端点鉴权:Bearer <AI4ALL_BRIDGE_SECRET>,须与中心一致。

    刻意不 import 中心 main 的同名依赖,保持 node-only 进程不拉起整个 central app。
    """
    expected = f"Bearer {settings.ai4all_bridge_secret}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge authorization",
        )


class NodeExecLoginStartRequest(BaseModel):
    account_id: str
    gateway_timeout_ms: int
    start_timeout_ms: int
    force: bool = False


class NodeExecLoginWaitRequest(BaseModel):
    account_id: str
    current_qr_data_url: Optional[str] = None
    gateway_timeout_ms: int
    wait_timeout_ms: int


class NodeExecLogoutRequest(BaseModel):
    account_id: str
    channel: str = DEFAULT_WEIXIN_CHANNEL
    timeout_ms: int


class NodeExecSendTextRequest(BaseModel):
    # 字段对齐 openclaw_gateway.send_weixin_text 签名；account_id 即 channel_account_id。
    to_user_id: str
    text: str
    gateway_timeout_ms: int
    account_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    session_key: Optional[str] = None
    channel: str = DEFAULT_WEIXIN_CHANNEL


def create_node_agent_app() -> FastAPI:
    """构建节点 agent 的 exec FastAPI app(登录 start/wait/logout + health)。"""
    app = FastAPI(title="ai4all-node-agent")

    @app.get("/health/live")
    def health_live() -> Dict[str, Any]:
        return {"status": "ok", "node_id": settings.node_id}

    @app.post("/node/exec/login/start")
    def exec_login_start(
        payload: NodeExecLoginStartRequest,
        _: None = Depends(_verify_bridge_auth),
    ) -> Dict[str, Any]:
        try:
            return openclaw_gateway.start_weixin_qr_login(
                account_id=payload.account_id,
                gateway_timeout_ms=payload.gateway_timeout_ms,
                start_timeout_ms=payload.start_timeout_ms,
                force=payload.force,
            )
        except OpenClawGatewayError as err:
            # 把原始 openclaw 错误文案放进 HTTP body,使中心 _exec_post 能透出
            # (中心据此分类,如 logout 的 unsupported vs failed)。
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))

    @app.post("/node/exec/login/wait")
    def exec_login_wait(
        payload: NodeExecLoginWaitRequest,
        _: None = Depends(_verify_bridge_auth),
    ) -> Dict[str, Any]:
        try:
            return openclaw_gateway.wait_weixin_qr_login(
                account_id=payload.account_id,
                current_qr_data_url=payload.current_qr_data_url,
                gateway_timeout_ms=payload.gateway_timeout_ms,
                wait_timeout_ms=payload.wait_timeout_ms,
            )
        except OpenClawGatewayError as err:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))

    @app.post("/node/exec/logout")
    def exec_logout(
        payload: NodeExecLogoutRequest,
        _: None = Depends(_verify_bridge_auth),
    ) -> Dict[str, Any]:
        try:
            return openclaw_gateway.logout_weixin_account(
                account_id=payload.account_id,
                channel=payload.channel,
                timeout_ms=payload.timeout_ms,
            )
        except OpenClawGatewayError as err:
            # 关键:openclaw "does not support logout" 经此进 body,中心据原文案判 unsupported。
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))

    @app.post("/node/exec/send/text")
    def exec_send_text(
        payload: NodeExecSendTextRequest,
        _: None = Depends(_verify_bridge_auth),
    ) -> Dict[str, Any]:
        # 中心 push 一条前台同步消息(暂态/欢迎语)给本节点持有的会话,本机调 openclaw 即时发。
        try:
            return openclaw_gateway.send_weixin_text(
                to_user_id=payload.to_user_id,
                text=payload.text,
                gateway_timeout_ms=payload.gateway_timeout_ms,
                account_id=payload.account_id,
                idempotency_key=payload.idempotency_key,
                session_key=payload.session_key,
                channel=payload.channel,
            )
        except OpenClawGatewayError as err:
            # 含 OpenClawRateLimited 子类;原始文案进 body 供中心透出/分类。
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))

    return app


# ----------------------------- 出站 pull 循环 -----------------------------

def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {settings.ai4all_bridge_secret}"}


def _central_base() -> str:
    return (settings.central_url or "").rstrip("/")


def _report_outbound_result(
    *, http_client: httpx.Client, outbound_message_id: Any, body: Dict[str, Any]
) -> None:
    url = f"{_central_base()}/node/outbound/{outbound_message_id}/result"
    resp = http_client.post(url, json=body, headers=_auth_headers())
    resp.raise_for_status()


def run_outbound_pull_once(*, http_client: httpx.Client) -> Dict[str, Any]:
    """一轮出站认领+发送+回报。

    1) POST /node/outbound/claim 领本节点待发消息;
    2) 逐条本机 openclaw send_weixin_text(限速/异常都回报 failed,由中心 stale 回收或下轮重领);
    3) POST /node/outbound/{id}/result 回报最终态。
    返回 {claimed, sent, failed} 计数;claim 网络失败返回 {claimed:0, error}。
    """
    node_id = (settings.node_id or "").strip()
    batch = int(getattr(settings, "outbound_pull_batch_size", 20) or 20)
    claim_url = f"{_central_base()}/node/outbound/claim"
    try:
        resp = http_client.post(
            claim_url,
            json={"node_id": node_id, "batch": batch},
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", []) or []
    except Exception as err:
        logger.warning("node outbound claim failed node=%s error=%s", node_id, err)
        return {"claimed": 0, "sent": 0, "failed": 0, "error": str(err)}

    sent = 0
    failed = 0
    for m in messages:
        mid = m.get("id")
        try:
            result = openclaw_gateway.send_weixin_text(
                to_user_id=m["to_user_id"],
                text=m["text"],
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=m.get("channel_account_id"),
                idempotency_key=m.get("idempotency_key"),
                session_key=m.get("session_key"),
                channel=m.get("channel") or DEFAULT_WEIXIN_CHANNEL,
            )
            gateway_message_id = (
                result.get("messageId") if isinstance(result, dict) else None
            )
            _report_outbound_result(
                http_client=http_client,
                outbound_message_id=mid,
                body={
                    "status": "sent",
                    "gateway_message_id": (
                        str(gateway_message_id) if gateway_message_id else None
                    ),
                },
            )
            sent += 1
        except Exception as err:
            # 限速(OpenClawRateLimited)与其它异常一律回 failed;中心据 attempts/stale 决定是否重领。
            logger.warning(
                "node outbound send failed node=%s outbound_id=%s error=%s",
                node_id, mid, err,
            )
            try:
                _report_outbound_result(
                    http_client=http_client,
                    outbound_message_id=mid,
                    body={"status": "failed", "error": str(err)[:500]},
                )
            except Exception as report_err:
                logger.warning(
                    "node outbound result(failed) report failed outbound_id=%s error=%s",
                    mid, report_err,
                )
            failed += 1
    return {"claimed": len(messages), "sent": sent, "failed": failed}


def send_heartbeat_once(*, http_client: httpx.Client) -> Optional[Dict[str, Any]]:
    """上报一次心跳,upsert 中心 access_nodes。失败仅告警,不抛。"""
    url = f"{_central_base()}/node/heartbeat"
    body: Dict[str, Any] = {
        "node_id": (settings.node_id or "").strip(),
        "base_url": (settings.node_base_url or "").strip() or None,
        "max_sessions": int(getattr(settings, "node_max_sessions", 0) or 0) or None,
    }
    try:
        resp = http_client.post(url, json=body, headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()
    except Exception as err:
        logger.warning("node heartbeat failed node=%s error=%s", body["node_id"], err)
        return None


def run_pull_loop(*, stop_event: threading.Event) -> None:
    """出站 pull 后台循环,直到 stop_event 置位。"""
    interval = max(0.1, float(getattr(settings, "outbound_pull_interval_seconds", 2.0) or 2.0))
    with httpx.Client(timeout=settings.openclaw_gateway_call_timeout_ms / 1000 + 10) as client:
        while not stop_event.is_set():
            try:
                run_outbound_pull_once(http_client=client)
            except Exception as err:  # 兜底:单轮异常不杀循环
                logger.exception("node pull loop iteration error: %s", err)
            stop_event.wait(interval)


def run_heartbeat_loop(*, stop_event: threading.Event, interval_seconds: float = 30.0) -> None:
    """心跳后台循环,直到 stop_event 置位。"""
    interval = max(1.0, float(interval_seconds))
    with httpx.Client(timeout=15.0) as client:
        while not stop_event.is_set():
            send_heartbeat_once(http_client=client)
            stop_event.wait(interval)
