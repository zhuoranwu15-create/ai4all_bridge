"""中心侧「按 node_id 派发」openclaw 登录/登出能力。

- **local node**(standalone,或 central+node 同机且 node_id 命中本机,或 node_id 为空=未分配)
  → 直调本机 `openclaw_gateway`,零网络跳,行为与单机一致(保证 standalone 零回归)。
- **remote node** → HTTP push 到该节点 `access_nodes.base_url` 的 `/node/exec/login/*` 端点
  (Bearer bridge_secret)。节点 agent 内部仍调本机 `openclaw_gateway`。

设计见 multi_node_access_refactor.md 附录 A.2。远程不可达/未登记一律抛
`OpenClawGatewayError`,使 main.py 既有 `except → set_binding_intent_error` 路径不变。
"""
import logging
from typing import Any, Dict, Optional

import httpx

from app import openclaw_gateway
from app.config import settings
from app.db import get_access_node
from app.openclaw_gateway import DEFAULT_WEIXIN_CHANNEL, OpenClawGatewayError

logger = logging.getLogger("ai4all.node_gateway")


def _is_local_node(node_id: Optional[str]) -> bool:
    """该 node_id 是否应由本进程直接执行(本机 openclaw)。"""
    # standalone:无远程节点概念,恒本机直调 → 保证零回归。
    if "standalone" in settings.role_set:
        return True
    if not settings.has_node_role:
        return False
    nid = (node_id or "").strip()
    # 空 node_id = 账号未分配 → 回落本机(迁移期 default_node_id=本机)。
    return nid == "" or nid == (settings.node_id or "").strip()


def _resolve_node_base_url(node_id: Optional[str]) -> str:
    nid = (node_id or "").strip()
    node = get_access_node(node_id=nid) if nid else None
    base = ((node or {}).get("base_url") or "").strip().rstrip("/")
    if not base:
        raise OpenClawGatewayError(
            f"node '{node_id}' has no registered base_url; cannot push login/logout"
        )
    return base


def _exec_post(*, path: str, base_url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST 到远程节点 exec 端点;HTTP/解析失败统一抛 OpenClawGatewayError。"""
    url = f"{base_url}{path}"
    headers = {"Authorization": f"Bearer {settings.ai4all_bridge_secret}"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as err:
        # 远程节点 exec 失败(如 openclaw "does not support logout")时,原始文案在
        # 响应体里。必须拼进异常,否则 main.py 的子串分类(unsupported vs failed)在
        # 远程路径会因 httpx 错误串不含原文案而误判为 failed。
        body = ""
        try:
            body = (err.response.text or "")[:500]
        except Exception:
            pass
        raise OpenClawGatewayError(
            f"node exec push failed ({url}): {err}; body={body}"
        ) from err
    except httpx.HTTPError as err:
        raise OpenClawGatewayError(f"node exec push failed ({url}): {err}") from err


def node_start_qr(
    *,
    node_id: Optional[str],
    account_id: str,
    gateway_timeout_ms: int,
    start_timeout_ms: int,
    force: bool = False,
) -> Dict[str, Any]:
    if _is_local_node(node_id):
        return openclaw_gateway.start_weixin_qr_login(
            account_id=account_id,
            gateway_timeout_ms=gateway_timeout_ms,
            start_timeout_ms=start_timeout_ms,
            force=force,
        )
    base = _resolve_node_base_url(node_id)
    return _exec_post(
        path="/node/exec/login/start",
        base_url=base,
        payload={
            "account_id": account_id,
            "gateway_timeout_ms": gateway_timeout_ms,
            "start_timeout_ms": start_timeout_ms,
            "force": force,
        },
        timeout=gateway_timeout_ms / 1000 + 5,
    )


def node_wait_qr(
    *,
    node_id: Optional[str],
    account_id: str,
    current_qr_data_url: Optional[str],
    gateway_timeout_ms: int,
    wait_timeout_ms: int,
) -> Dict[str, Any]:
    if _is_local_node(node_id):
        return openclaw_gateway.wait_weixin_qr_login(
            account_id=account_id,
            current_qr_data_url=current_qr_data_url,
            gateway_timeout_ms=gateway_timeout_ms,
            wait_timeout_ms=wait_timeout_ms,
        )
    base = _resolve_node_base_url(node_id)
    # wait 是长轮询:HTTP 客户端超时必须 > wait_timeout_ms(语义同今天 subprocess 超时)。
    return _exec_post(
        path="/node/exec/login/wait",
        base_url=base,
        payload={
            "account_id": account_id,
            "current_qr_data_url": current_qr_data_url,
            "gateway_timeout_ms": gateway_timeout_ms,
            "wait_timeout_ms": wait_timeout_ms,
        },
        timeout=wait_timeout_ms / 1000 + 10,
    )


def node_logout(
    *,
    node_id: Optional[str],
    account_id: str,
    channel: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    if _is_local_node(node_id):
        return openclaw_gateway.logout_weixin_account(
            account_id=account_id,
            channel=channel,
            timeout_ms=timeout_ms,
        )
    base = _resolve_node_base_url(node_id)
    return _exec_post(
        path="/node/exec/logout",
        base_url=base,
        payload={
            "account_id": account_id,
            "channel": channel,
            "timeout_ms": timeout_ms,
        },
        timeout=timeout_ms / 1000 + 5,
    )


def node_send_text(
    *,
    node_id: Optional[str],
    to_user_id: str,
    text: str,
    gateway_timeout_ms: int,
    account_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    session_key: Optional[str] = None,
    channel: str = DEFAULT_WEIXIN_CHANNEL,
) -> Dict[str, Any]:
    """按账号归属节点发一条前台同步消息(暂态/欢迎语)。

    与 node_logout 同构:本机账号直调 openclaw,远程账号 HTTP push 到归属节点
    `/node/exec/send/text`(中心不持有远程会话,不能直接 send)。远程不可达/未登记一律抛
    OpenClawGatewayError,由调用方决定丢弃(暂态)或回落 enqueue(欢迎语)。
    """
    if _is_local_node(node_id):
        return openclaw_gateway.send_weixin_text(
            to_user_id=to_user_id,
            text=text,
            gateway_timeout_ms=gateway_timeout_ms,
            account_id=account_id,
            idempotency_key=idempotency_key,
            session_key=session_key,
            channel=channel,
        )
    base = _resolve_node_base_url(node_id)
    return _exec_post(
        path="/node/exec/send/text",
        base_url=base,
        payload={
            "to_user_id": to_user_id,
            "text": text,
            "gateway_timeout_ms": gateway_timeout_ms,
            "account_id": account_id,
            "idempotency_key": idempotency_key,
            "session_key": session_key,
            "channel": channel,
        },
        timeout=gateway_timeout_ms / 1000 + 5,
    )
