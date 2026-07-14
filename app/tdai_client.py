"""TDAI Gateway HTTP client.

Hot-path recall is synchronous: called from the sync turn handler running in FastAPI's
thread pool, so blocking I/O is acceptable here without asyncio.to_thread.
_recall_sync() does the actual HTTP call; recall_async() wraps it for async callers.

After-turn capture and unbind wipe are async: dispatched to background_loop as tasks,
consistent with write_memory and other after-turn work.

Isolation contract: every call MUST carry tdai_session_key(account_id) == "ai4all:{account_id}".
Never use OpenClaw session_key, channel_account_id, or any other identifier as the TDAI key.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 多租户安全闸门：启动探针（verify_multitenant）确认网关退回共享库模式时置 True，
# search_allowed() 读它强制关闭主动检索（L1 会跨账号串号）。recall/capture 不受影响。
_MT_UNSAFE: bool = False


def tdai_session_key(account_id: str) -> str:
    """Stable TDAI session key scoped to one AI4ALL account."""
    return f"ai4all:{account_id}"


def _is_recall_allowed(account_id: str) -> bool:
    """Return True if account_id is in the recall allowlist (comma-separated)."""
    raw = getattr(settings, "tdai_recall_account_allowlist", "")
    if not raw:
        return False
    return account_id in {s.strip() for s in raw.split(",") if s.strip()}


def _is_search_allowed(account_id: str) -> bool:
    """Return True if account_id is in the search allowlist (comma-separated)."""
    raw = getattr(settings, "tdai_search_account_allowlist", "")
    if not raw:
        return False
    return account_id in {s.strip() for s in raw.split(",") if s.strip()}


def search_allowed(account_id: str, *, volume_eligible: bool = False) -> bool:
    """主动检索工具是否对该账号开放：总开关 + search 开关 + 多租户安全闸门 + 准入。

    这是 gating 的唯一判定点：turn 侧据此决定是否把两个工具放进默认工具集。
    准入 = allowlist 命中 OR volume_eligible（账号累计消息数越过阈值，由调用方算好传入）。
    总开关 / search 开关 / _MT_UNSAFE 三道硬闸门优先，任一不满足直接关闭。
    """
    if not getattr(settings, "tdai_enabled", False):
        return False
    if not getattr(settings, "tdai_search_enabled", False):
        return False
    if _MT_UNSAFE:
        return False
    return _is_search_allowed(account_id) or volume_eligible


def _auth_header() -> Dict[str, str]:
    """Bearer auth header, or empty dict when no api key is configured.

    Gateway auth is optional (local dev runs without a key: `if (!expected) return true`).
    Emitting `Authorization: Bearer ` with an empty key makes httpx raise
    `Illegal header value`, which would silently break every recall/capture/wipe.
    """
    key = (getattr(settings, "tdai_gateway_api_key", "") or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# Sync recall (hot path)
# ---------------------------------------------------------------------------

def _recall_sync(*, account_id: str, query: str) -> Dict[str, Any]:
    """HTTP POST /recall with a strict timeout.  Returns {} on timeout/failure.

    New httpx.Client per call: on localhost the TCP overhead is <1 ms and is
    negligible vs. the recall timeout (settings.tdai_recall_timeout_seconds,
    default 0.5 s).  A module-level singleton would require extra locking and
    complicate test patching.

    Recall latency is dominated by the gateway's DashScope embedding round-trip
    (measured ~200-860 ms, highly variable), so the timeout intentionally clips
    the slow tail and cold hits — a miss degrades gracefully (no injection) and
    never blocks the reply.
    """
    timeout = float(getattr(settings, "tdai_recall_timeout_seconds", 0.5))
    key = tdai_session_key(account_id)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{settings.tdai_gateway_url}/recall",
                headers=_auth_header(),
                json={"query": query, "session_key": key},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        logger.debug("tdai recall timeout account=%s", account_id)
        return {}
    except Exception as exc:
        logger.warning("tdai recall failed account=%s error=%s", account_id, exc)
        return {}


async def recall_async(*, account_id: str, query: str) -> Dict[str, Any]:
    """Async wrapper around _recall_sync for callers in an async context."""
    return await asyncio.to_thread(_recall_sync, account_id=account_id, query=query)


def recall(*, account_id: str, query: str, volume_eligible: bool = False) -> Dict[str, Any]:
    """Gated sync entry point for the turn hot path.

    准入 = recall allowlist 命中 OR volume_eligible（账号累计消息数越过阈值，由调用方算好传入）。
    Returns {} when TDAI is disabled, recall is disabled, or the account passes neither
    the allowlist nor the volume threshold.  Never raises.
    """
    if not getattr(settings, "tdai_enabled", False):
        return {}
    if not getattr(settings, "tdai_recall_enabled", True):
        return {}
    if not (_is_recall_allowed(account_id) or volume_eligible):
        return {}
    return _recall_sync(account_id=account_id, query=query)


# ---------------------------------------------------------------------------
# Sync active search (model-initiated, inside the tool loop)
# ---------------------------------------------------------------------------

def _search_sync(endpoint: str, *, account_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /search/* with a strict timeout.  Returns {} on timeout/failure, never raises.

    session_key 强制注入 tdai_session_key(account_id)（隔离契约）：memory 端仅用于路由到该
    账号独立库，conversation 端另做 session 后置过滤。调用发生在模型 tool loop 中途，超时/失败
    降级为空结果、由 handler 转成 benign 文本，不打断本轮生成（与 recall 一个哲学）。
    """
    timeout = float(getattr(settings, "tdai_search_timeout_seconds", 2.0))
    body = {**payload, "session_key": tdai_session_key(account_id)}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{settings.tdai_gateway_url}{endpoint}",
                headers=_auth_header(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        logger.debug("tdai search timeout endpoint=%s account=%s", endpoint, account_id)
        return {}
    except Exception as exc:
        logger.warning("tdai search failed endpoint=%s account=%s error=%s", endpoint, account_id, exc)
        return {}


def search_memories(
    *, account_id: str, query: str, limit: int, type: Optional[str] = None, scene: Optional[str] = None
) -> Dict[str, Any]:
    """POST /search/memories — 查 L1 结构化记忆。响应 {results:str, total:int, strategy:str}。"""
    payload: Dict[str, Any] = {"query": query, "limit": limit}
    if type:
        payload["type"] = type
    if scene:
        payload["scene"] = scene
    return _search_sync("/search/memories", account_id=account_id, payload=payload)


def search_conversations(*, account_id: str, query: str, limit: int) -> Dict[str, Any]:
    """POST /search/conversations — 查 L0 原始逐轮对话。响应 {results:str, total:int}。"""
    return _search_sync(
        "/search/conversations", account_id=account_id, payload={"query": query, "limit": limit}
    )


def verify_multitenant() -> Optional[bool]:
    """启动探针：确认网关处于 multiTenant 强隔离模式。

    利用「multiTenant=true 且缺 session_key → 400」这一网关契约：POST /search/conversations
    带 query 但**不带 session_key**。
    - 400 → 强隔离模式，安全，返回 True。
    - 200 → 共享库模式（L1 会跨账号串号），返回 False。
    - 网络错误/超时 → 返回 None（best-effort，不阻塞启动）。
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{settings.tdai_gateway_url}/search/conversations",
                headers=_auth_header(),
                json={"query": "__mt_probe__"},
            )
    except Exception as exc:
        logger.warning("tdai verify_multitenant probe failed (best-effort): %s", exc)
        return None
    if resp.status_code == 400:
        return True
    if resp.status_code == 200:
        return False
    logger.warning("tdai verify_multitenant unexpected status=%s", resp.status_code)
    return None


def mark_multitenant_unsafe() -> None:
    """置多租户安全闸门为不安全，强制关闭主动检索（recall/capture 不受影响）。"""
    global _MT_UNSAFE
    _MT_UNSAFE = True


# ---------------------------------------------------------------------------
# Async capture (after-turn, dispatched to background_loop)
# ---------------------------------------------------------------------------

async def capture_turn(
    *,
    account_id: str,
    session_id: int,
    user_content: str,
    assistant_content: str,
) -> None:
    """POST /capture — best-effort; never raises, logs warning on failure.

    Dispatch via background_loop.call_soon_threadsafe(background_loop.create_task, capture_turn(...)).
    Only visible user/assistant text should be passed; never pass system prompt,
    tool results, or TDAI recall context.
    """
    if not getattr(settings, "tdai_enabled", False):
        return
    if not getattr(settings, "tdai_capture_enabled", True):
        return

    timeout = float(getattr(settings, "tdai_capture_timeout_seconds", 2.0))
    key = tdai_session_key(account_id)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.tdai_gateway_url}/capture",
                headers=_auth_header(),
                json={
                    "session_key": key,
                    "session_id": str(session_id),
                    "user_content": user_content,
                    "assistant_content": assistant_content,
                },
            )
            resp.raise_for_status()
            logger.debug(
                "tdai capture ok account=%s session=%s", account_id, session_id
            )
    except Exception as exc:
        logger.warning(
            "tdai capture failed account=%s session=%s error=%s",
            account_id,
            session_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Async namespace wipe (unbind path, dispatched to background_loop)
# ---------------------------------------------------------------------------

async def namespace_wipe(*, account_id: str) -> bool:
    """POST /namespace/wipe — call after DB transaction commits, best-effort.

    TDAI is a derived cache of AI4ALL messages and can always be re-seeded, so
    wipe is safe under both keep_memories=True and keep_memories=False.
    Returns True on success, False on failure.
    """
    if not getattr(settings, "tdai_enabled", False):
        return True  # no-op when TDAI disabled

    timeout = 5.0
    key = tdai_session_key(account_id)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.tdai_gateway_url}/namespace/wipe",
                headers=_auth_header(),
                json={"session_key": key},
            )
            resp.raise_for_status()
            logger.info("tdai namespace_wipe ok account=%s", account_id)
            return True
    except Exception as exc:
        logger.warning(
            "tdai namespace_wipe failed account=%s error=%s", account_id, exc
        )
        return False
