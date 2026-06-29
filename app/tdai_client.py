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
from typing import Any, Dict

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def tdai_session_key(account_id: str) -> str:
    """Stable TDAI session key scoped to one AI4ALL account."""
    return f"ai4all:{account_id}"


def _is_recall_allowed(account_id: str) -> bool:
    """Return True if account_id is in the recall allowlist (comma-separated)."""
    raw = getattr(settings, "tdai_recall_account_allowlist", "")
    if not raw:
        return False
    return account_id in {s.strip() for s in raw.split(",") if s.strip()}


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
    timeout = float(getattr(settings, "tdai_recall_timeout_seconds", 0.2))
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


def recall(*, account_id: str, query: str) -> Dict[str, Any]:
    """Gated sync entry point for the turn hot path.

    Returns {} when TDAI is disabled, recall is disabled, or the account is not
    in the recall allowlist.  Never raises.
    """
    if not getattr(settings, "tdai_enabled", False):
        return {}
    if not getattr(settings, "tdai_recall_enabled", True):
        return {}
    if not _is_recall_allowed(account_id):
        return {}
    return _recall_sync(account_id=account_id, query=query)


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
