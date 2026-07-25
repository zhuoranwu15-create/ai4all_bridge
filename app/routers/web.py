"""Web 前端面向用户的路由（/web/*）+ 其独占的绑定编排 / FAQ / OTP helper。

从 app.main 拆出（结构优化，函数体逐字保留）。settings 在本模块绑定，
测试需 patch "app.routers.web.settings" 及 _schedule_binding_wait/verify_captcha/
send_otp/generate_completion 等本模块名（patch where it's used）。
"""
import json
import hashlib
import threading
import asyncio
import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from app.config import settings
from app.bootstrap.runtime import get_background_loop
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.routers.deps import _require_session
from app.platform.gateways import node_gateway
from app.platform.auth.captcha import verify_captcha
from app.db import SessionPrincipal, count_verifications_last_hour, create_binding_intent, create_faq_message, create_phone_verification, create_platform_user_session, get_account_onboarding_state, get_binding_intent, get_campaign_code, record_campaign_visit, get_latest_active_verification, get_latest_subscription_for_user, get_or_create_default_ai4all_account_for_user, get_or_create_personal_referral_code_for_user, get_platform_user, get_wallet_summary, increment_verify_attempts, invalidate_other_verifications_for_phone, invalidate_verification, like_faq_message, list_channel_bindings_for_account, list_published_faq_messages, list_wallet_ledger, mark_referral_relationship_bound, normalize_phone, preview_referral_code, reenable_proactive_after_rebind, register_platform_user_with_referral, resolve_node_for_account, set_account_onboarding_state, set_binding_intent_error, set_verification_verified, unbind_account_channel, unbind_and_wipe_account, update_binding_intent, upsert_channel_binding
from app.agent_runtime.llm.service import generate_completion
from app.products.zhaoxi.application.onboarding import ONBOARDING_STEP1_SENT, ONBOARDING_WELCOME_TEXT
from app.platform.quota.rate_limiter import RateLimiter
from app.platform.auth.sms import generate_otp, send_otp
from typing import Any, Optional

logger = logging.getLogger("ai4all")
router = APIRouter()


_referral_preview_rate_limiter = RateLimiter()


_REFERRAL_PREVIEW_RPM = 60


# 落地页曝光 beacon 限流器：单 IP 每分钟上限，超限静默丢弃（不返回 429，不给刷量者信号）。
_campaign_visit_rate_limiter = RateLimiter()
_CAMPAIGN_VISIT_RPM = 120


class WebRegisterRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str
    invite_code: Optional[str] = None


class WebRegisterAndBindingIntentRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str
    channel: Optional[str] = "openclaw-weixin"
    invite_code: Optional[str] = None
    campaign_code: Optional[str] = None


class SendOtpRequest(BaseModel):
    phone: str
    captcha_verify_param: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str


class WebCreateBindingIntentRequest(BaseModel):
    channel: Optional[str] = "openclaw-weixin"


class WebUnbindRequest(BaseModel):
    keep_memories: bool


class FAQMessageRequest(BaseModel):
    author_name: Optional[str] = Field(default=None, max_length=40)
    content: str = Field(min_length=1, max_length=1000)


class FAQLikeRequest(BaseModel):
    voter_token: Optional[str] = Field(default=None, max_length=120)


def _normalize_openclaw_weixin_account_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    if "openclaw-weixin:" in text:
        text = text.split("openclaw-weixin:", 1)[1].split(":", 1)[0].strip()
    if text.endswith("@im.bot"):
        return f"{text[:-7]}-im-bot"
    if text.endswith("-im-bot"):
        return text
    if text.endswith("@im.wechat"):
        return f"{text[:-10]}-im-wechat"
    if text.endswith("-im-wechat"):
        return text
    return None


def _collect_openclaw_weixin_logout_targets(bindings: list[dict]) -> list[str]:
    targets = []
    seen = set()
    for binding in bindings:
        if binding.get("channel") != "openclaw-weixin":
            continue
        raw_identity = binding.get("raw_identity") or {}
        candidates = [
            binding.get("channel_account_id"),
            binding.get("session_key"),
            raw_identity.get("channel_account_id"),
            raw_identity.get("accountId"),
            raw_identity.get("account_id"),
            raw_identity.get("session_key"),
            raw_identity.get("openclaw_session_key_account_id"),
        ]
        for candidate in candidates:
            target = _normalize_openclaw_weixin_account_id(candidate)
            if target and target not in seen:
                targets.append(target)
                seen.add(target)
    return targets


def _cleanup_openclaw_weixin_accounts(bindings: list[dict], *, node_id: Optional[str] = None) -> dict:
    targets = _collect_openclaw_weixin_logout_targets(bindings)
    if not targets:
        return {
            "status": "skipped",
            "reason": "no_openclaw_weixin_binding",
            "attempts": [],
        }

    attempts = []
    for target in targets:
        try:
            # 多机:登出在会话所在节点本机执行;node_id 为空(standalone/同机)→ 本机直调。
            result = node_gateway.node_logout(
                node_id=node_id,
                account_id=target,
                channel="openclaw-weixin",
                timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            )
            attempts.append(
                {
                    "channel": "openclaw-weixin",
                    "account_id": target,
                    "status": "ok",
                    "result": result,
                }
            )
        except Exception as err:
            error = str(err)
            status = "unsupported" if "does not support logout" in error else "failed"
            logger.warning("OpenClaw Weixin logout failed account=%s error=%s", target, error)
            attempts.append(
                {
                    "channel": "openclaw-weixin",
                    "account_id": target,
                    "status": status,
                    "error": error,
                }
            )

    if all(item["status"] == "ok" for item in attempts):
        status = "ok"
    elif all(item["status"] == "unsupported" for item in attempts):
        status = "unsupported"
    elif any(item["status"] == "ok" for item in attempts):
        status = "partial_failed"
    else:
        status = "failed"
    return {"status": status, "attempts": attempts}


def _complete_binding_intent_from_wait_result(binding_intent: dict, result: dict) -> None:
    raw_channel_account_id = result.get("accountId") or result.get("channel_account_id")
    channel_account_id = str(raw_channel_account_id).strip() if raw_channel_account_id else None
    raw_result = {
        **result,
        "channel_account_id": channel_account_id,
        "binding_intent_id": binding_intent["id"],
    }
    if result.get("connected") and channel_account_id:
        update_binding_intent(
            binding_intent_id=binding_intent["id"],
            status="completed",
            channel_account_id=channel_account_id,
            raw_result=raw_result,
            completed=True,
            error=None,
        )
        # sender_id (WeChat OpenID) is not available at binding time — it only
        # arrives with the user's first inbound message. Proactive welcome is
        # skipped until then (see _send_onboarding_proactive_welcome).
        upsert_channel_binding(
            account_id=binding_intent["account_id"],
            channel=binding_intent["channel"],
            session_key=binding_intent["openclaw_login_session_key"],
            channel_account_id=channel_account_id,
            sender_id=None,
            chat_id=None,
            raw_identity={
                "binding_intent_id": binding_intent["id"],
                "platform_user_id": binding_intent["platform_user_id"],
                "ai4all_account_id": binding_intent["account_id"],
                "openclaw_login_session_key": binding_intent["openclaw_login_session_key"],
                "channel_account_id": channel_account_id,
            },
        )
        try:
            mark_referral_relationship_bound(
                invitee_platform_user_id=binding_intent["platform_user_id"],
                account_id=binding_intent["account_id"],
            )
        except Exception as err:
            logger.warning(
                "referral relationship bound update failed intent=%s account=%s error=%s",
                binding_intent["id"],
                binding_intent["account_id"],
                err,
            )
        reenable_proactive_after_rebind(account_id=binding_intent["account_id"])
        return

    if result.get("alreadyConnected") and channel_account_id:
        update_binding_intent(
            binding_intent_id=binding_intent["id"],
            status="completed",
            channel_account_id=channel_account_id,
            raw_result=raw_result,
            completed=True,
            error=None,
        )
        upsert_channel_binding(
            account_id=binding_intent["account_id"],
            channel=binding_intent["channel"],
            session_key=binding_intent["openclaw_login_session_key"],
            channel_account_id=channel_account_id,
            sender_id=None,
            chat_id=None,
            raw_identity={
                "binding_intent_id": binding_intent["id"],
                "platform_user_id": binding_intent["platform_user_id"],
                "ai4all_account_id": binding_intent["account_id"],
                "openclaw_login_session_key": binding_intent["openclaw_login_session_key"],
                "channel_account_id": channel_account_id,
                "already_connected": True,
            },
        )
        try:
            mark_referral_relationship_bound(
                invitee_platform_user_id=binding_intent["platform_user_id"],
                account_id=binding_intent["account_id"],
            )
        except Exception as err:
            logger.warning(
                "referral relationship bound update failed intent=%s account=%s error=%s",
                binding_intent["id"],
                binding_intent["account_id"],
                err,
            )
        reenable_proactive_after_rebind(account_id=binding_intent["account_id"])
        return

    status = "already_connected" if result.get("alreadyConnected") else "failed"
    set_binding_intent_error(
        binding_intent_id=binding_intent["id"],
        status=status,
        error=str(result.get("message") or status),
        raw_result=raw_result,
    )


async def _wait_for_binding_intent(binding_intent_id: str) -> None:
    binding_intent = get_binding_intent(binding_intent_id=binding_intent_id)
    if binding_intent is None:
        return
    try:
        result = await asyncio.to_thread(
            node_gateway.node_wait_qr,
            node_id=binding_intent.get("node_id"),
            account_id=binding_intent["openclaw_login_session_key"],
            current_qr_data_url=binding_intent.get("qr_data_url"),
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            wait_timeout_ms=settings.openclaw_login_wait_timeout_ms,
        )
    except Exception as err:
        logger.exception("OpenClaw QR wait failed intent=%s", binding_intent_id)
        set_binding_intent_error(
            binding_intent_id=binding_intent_id,
            status="failed",
            error=str(err),
        )
        return
    latest = get_binding_intent(binding_intent_id=binding_intent_id)
    if latest is None:
        return
    _complete_binding_intent_from_wait_result(latest, result)

    # After successful binding: send activation self-message + schedule onboarding welcome.
    latest_after = get_binding_intent(binding_intent_id=binding_intent_id)
    if latest_after and latest_after.get("status") == "completed":
        account_id = latest_after["account_id"]
        channel = latest_after["channel"]
        channel_account_id = latest_after.get("channel_account_id")
        session_key = latest_after.get("openclaw_login_session_key")

        loop = asyncio.get_event_loop()
        loop.call_later(
            5.0,
            lambda: loop.create_task(
                _send_onboarding_welcome_if_pending(
                    account_id=account_id,
                    channel=channel,
                    channel_account_id=channel_account_id,
                    session_key=session_key,
                )
            ),
        )


_ONBOARDING_WELCOME_TEXT = ONBOARDING_WELCOME_TEXT


async def _send_onboarding_welcome_if_pending(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    session_key: Optional[str],
) -> None:
    """Send the onboarding welcome message if the user hasn't sent their first message yet.

    Best-effort: requires a sender_id from channel_bindings. If not available,
    the user will trigger onboarding with their first inbound message instead.
    """
    try:
        state = await asyncio.to_thread(
            get_account_onboarding_state, account_id=account_id
        )
        if state != "pending":
            logger.info(
                "onboarding proactive skipped account=%s state=%s (already advanced)",
                account_id, state,
            )
            return

        # Look up the sendable peer from channel bindings (populated when user first messages)
        bindings = await asyncio.to_thread(
            list_channel_bindings_for_account, account_id=account_id
        )
        to_user_id = None
        resolved_session_key = session_key
        for b in bindings:
            peer = b.get("chat_id") or b.get("sender_id")
            if peer:
                to_user_id = peer
                resolved_session_key = b.get("session_key") or session_key
                break

        if not to_user_id:
            logger.info(
                "onboarding proactive skipped account=%s reason=no_weixin_peer_yet "
                "(user will trigger onboarding with first inbound message)",
                account_id,
            )
            return

        # 多机:统一经 node_send_text 按归属节点即时发(本机直调/远程 push)。
        # best-effort:push 失败由外层 except 记录并跳过(状态不置 step1_sent,首条入站再触发)。
        await asyncio.to_thread(
            node_gateway.node_send_text,
            node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
            to_user_id=to_user_id,
            text=_ONBOARDING_WELCOME_TEXT,
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            account_id=channel_account_id,
            # 固定幂等键:与首条入站 welcome 共用,网关去重防并发重复欢迎。
            idempotency_key=f"onboarding-welcome-{account_id}",
            session_key=resolved_session_key,
            channel=channel,
        )
        await asyncio.to_thread(
            set_account_onboarding_state, account_id=account_id, state=ONBOARDING_STEP1_SENT
        )
        logger.info(
            "onboarding proactive welcome sent account=%s to=%s state->step1_sent",
            account_id, to_user_id,
        )
    except Exception as err:
        logger.exception(
            "onboarding proactive welcome failed account=%s error=%s", account_id, err
        )


def _schedule_binding_wait(binding_intent_id: str) -> None:
    loop = get_background_loop()
    if loop is None:
        # 不再静默返回：无后台事件循环时绑定等待不会被调度，记 warning 便于排查。
        logger.warning(
            "binding wait not scheduled: no background loop binding_intent=%s",
            binding_intent_id,
        )
        return
    loop.call_soon_threadsafe(
        loop.create_task,
        _wait_for_binding_intent(binding_intent_id),
    )


def _start_openclaw_qr_for_binding(binding_intent: dict) -> dict:
    if not settings.openclaw_login_auto_start:
        return binding_intent
    try:
        start_result = node_gateway.node_start_qr(
            node_id=binding_intent.get("node_id"),
            account_id=binding_intent["openclaw_login_session_key"],
            gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
            start_timeout_ms=settings.openclaw_login_start_timeout_ms,
            force=False,
        )
    except Exception as err:
        logger.warning("OpenClaw QR start failed intent=%s error=%s", binding_intent["id"], err)
        failed = set_binding_intent_error(
            binding_intent_id=binding_intent["id"],
            status="failed",
            error=str(err),
        )
        return failed or binding_intent

    session_key = str(start_result.get("sessionKey") or binding_intent["openclaw_login_session_key"])
    updated = update_binding_intent(
        binding_intent_id=binding_intent["id"],
        status="qr_created" if start_result.get("qrDataUrl") else "failed",
        openclaw_login_session_key=session_key,
        qr_data_url=start_result.get("qrDataUrl"),
        raw_result={
            "start": start_result,
            "binding_intent_id": binding_intent["id"],
            "openclaw_login_session_key": session_key,
        },
        error=None if start_result.get("qrDataUrl") else str(start_result.get("message") or "QR not returned"),
    )
    updated = updated or binding_intent
    if updated.get("qr_data_url"):
        _schedule_binding_wait(updated["id"])
    return updated


_FAQ_MODERATION_SYSTEM_PROMPT = """
你是公开网站留言区的安全审核器。请只输出 JSON，不要输出解释。
判断用户留言是否适合直接公开展示。风险包括但不限于：政治敏感、色情低俗、仇恨歧视、暴力威胁、自伤诱导、违法犯罪、垃圾广告、隐私泄露、辱骂骚扰。
输出格式：
{"safe": true|false, "categories": ["..."], "reason": "..."}
safe=true 表示可以直接公开；safe=false 表示需要人工审核。
""".strip()


def _clean_faq_text(value: Optional[str], *, max_len: int) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_len]


def _parse_faq_moderation_json(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty moderation response")
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("moderation response must be an object")
    return payload


def _moderate_faq_message(content: str) -> dict:
    """Use a one-shot LLM moderation pass for public FAQ messages."""
    try:
        from app.agent_runtime.llm.providers import TASK_WEB_COMPLETION, tier_for_task

        raw = generate_completion(
            [
                {"role": "system", "content": _FAQ_MODERATION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            tier=tier_for_task(TASK_WEB_COMPLETION),
        )
        payload = _parse_faq_moderation_json(raw)
    except Exception as err:
        logger.warning("faq moderation failed: %s", err)
        return {
            "status": "pending",
            "moderation_status": "failed",
            "reason": "moderation_failed",
            "categories": ["moderation_failed"],
        }

    safe = bool(payload.get("safe") is True)
    categories = payload.get("categories") or []
    if not isinstance(categories, list):
        categories = [str(categories)]
    cleaned_categories = [
        str(item).strip()[:40]
        for item in categories
        if str(item).strip()
    ][:8]
    reason = _clean_faq_text(payload.get("reason"), max_len=200)
    return {
        "status": "published" if safe else "pending",
        "moderation_status": "safe" if safe else "needs_review",
        "reason": reason or ("safe" if safe else "needs_review"),
        "categories": cleaned_categories,
    }


def _faq_message_for_public(message: dict) -> dict:
    item = {
        "id": message["id"],
        "parent_id": message.get("parent_id"),
        "author_name": message.get("author_name") or "匿名用户",
        "content": message["content"],
        "like_count": int(message.get("like_count") or 0),
        "reply_count": int(message.get("reply_count") or 0),
        "published_at": message.get("published_at"),
        "created_at": message.get("created_at"),
    }
    if "replies" in message:
        item["replies"] = [_faq_message_for_public(reply) for reply in message["replies"]]
    return item


def _faq_voter_key(*, request: Request, voter_token: Optional[str]) -> str:
    token = str(voter_token or "").strip()
    if token:
        source = f"token:{token[:120]}"
    else:
        host = request.client.host if request.client else ""
        user_agent = str(request.headers.get("user-agent") or "")[:200]
        source = f"fallback:{host}:{user_agent}"
    return hashlib.sha256(f"faq-like:{source}".encode("utf-8")).hexdigest()


def _faq_message_kind(message: dict) -> str:
    return "回复" if message.get("parent_id") else "留言"


def _send_feishu_website_webhook(*, title: str, message: dict) -> None:
    """Send a best-effort Feishu website webhook notification."""
    webhook_url = str(getattr(settings, "feishu_website_webhook_url", "") or "").strip()
    if not webhook_url:
        return
    content = str(message.get("content") or "")
    preview = content[:300] + ("..." if len(content) > 300 else "")
    text = (
        f"{title}\n"
        f"ID: {message.get('id')}\n"
        f"作者: {message.get('author_name') or '匿名用户'}\n"
        f"类型: {_faq_message_kind(message)}\n"
        f"状态: {message.get('status')}\n"
        f"审核: {message.get('moderation_status')}\n"
        f"原因: {message.get('moderation_reason') or '无'}\n"
        f"内容: {preview}"
    )
    try:
        response = httpx.post(
            webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=5.0,
        )
        response.raise_for_status()
    except Exception as err:
        logger.warning("feishu website webhook notification failed: %s", err)


def _notify_faq_message_submitted(message: dict) -> None:
    _send_feishu_website_webhook(title="FAQ 用户发表留言", message=message)


def _notify_pending_faq_message(message: dict) -> None:
    _send_feishu_website_webhook(title="FAQ 留言需要审核待处理", message=message)


def _create_faq_message_from_payload(
    *,
    payload: FAQMessageRequest,
    parent_id: Optional[str] = None,
    request: Optional[Request] = None,
) -> dict:
    content = str(payload.content or "").strip()
    author_name = _clean_faq_text(payload.author_name, max_len=40)
    moderation = _moderate_faq_message(content)
    metadata = {"source": "faq_page"}
    if request is not None:
        user_agent = _clean_faq_text(request.headers.get("user-agent"), max_len=200)
        if user_agent:
            metadata["user_agent"] = user_agent
    try:
        message = create_faq_message(
            parent_id=parent_id,
            author_name=author_name,
            content=content,
            status=moderation["status"],
            moderation_status=moderation["moderation_status"],
            moderation_reason=moderation["reason"],
            moderation_categories=moderation["categories"],
            metadata=metadata,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    _notify_faq_message_submitted(message)
    if message["status"] == "pending":
        _notify_pending_faq_message(message)
    return {
        "status": "ok",
        "message_status": message["status"],
        "message": _faq_message_for_public(message) if message["status"] == "published" else {
            "id": message["id"],
            "status": message["status"],
        },
    }


@router.get("/web/config")
def web_config() -> dict:
    """Return public frontend configuration; never include server secrets."""
    captcha_scene_id = str(getattr(settings, "aliyun_captcha_scene_id", "") or "").strip()
    captcha_prefix = str(getattr(settings, "aliyun_captcha_prefix", "") or "").strip()
    return {
        "captcha": {
            "provider": "aliyun",
            "scene_id": captcha_scene_id,
            "prefix": captcha_prefix,
            "configured": bool(captcha_scene_id and captcha_prefix),
        },
        "registration": {
            "mode": "open",
            "invite_required": False,
            "invite_code_param": "invite_code",
            "campaign_code_param": "campaign_code",
        },
    }


@router.get("/web/referral-codes/{code}/preview")
def web_referral_code_preview(code: str, request: Request) -> dict:
    client_host = request.client.host if request.client else "unknown"
    if not _referral_preview_rate_limiter.check_rpm(
        f"referral-preview:{client_host}",
        _REFERRAL_PREVIEW_RPM,
        window_seconds=60.0,
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    preview = preview_referral_code(code=code, expected_app_id=ZHAOXI_APP_ID)
    if not preview.get("valid"):
        return {
            "valid": False,
            "reason": preview.get("reason") or "invalid",
        }
    return {
        "valid": True,
        "code": preview["code"],
        "code_type": preview.get("code_type"),
        "inviter_display_name": preview.get("inviter_display_name"),
    }


@router.get("/web/faq/messages")
def web_list_faq_messages(limit: int = 50, replies_per_parent: int = 20) -> dict:
    messages = list_published_faq_messages(
        limit=limit,
        replies_per_parent=replies_per_parent,
    )
    return {
        "status": "ok",
        "messages": [_faq_message_for_public(message) for message in messages],
    }


@router.post("/web/faq/messages")
def web_create_faq_message(payload: FAQMessageRequest, request: Request) -> dict:
    return _create_faq_message_from_payload(payload=payload, request=request)


@router.post("/web/faq/messages/{message_id}/replies")
def web_create_faq_reply(
    message_id: str,
    payload: FAQMessageRequest,
    request: Request,
) -> dict:
    return _create_faq_message_from_payload(
        payload=payload,
        parent_id=message_id,
        request=request,
    )


@router.post("/web/faq/messages/{message_id}/like")
def web_like_faq_message(
    message_id: str,
    request: Request,
    payload: Optional[FAQLikeRequest] = None,
) -> dict:
    voter_key = _faq_voter_key(
        request=request,
        voter_token=payload.voter_token if payload else None,
    )
    message = like_faq_message(message_id=message_id, voter_key=voter_key)
    if message is None:
        raise HTTPException(status_code=404, detail="faq message not found")
    return {
        "status": "ok",
        "liked": bool(message.get("liked")),
        "message": _faq_message_for_public(message),
    }


_otp_send_master_lock = threading.Lock()


_otp_send_locks: dict[str, threading.Lock] = {}


def _otp_send_lock_for(phone: str) -> threading.Lock:
    """Return (lazily creating) a per-phone in-process lock for OTP send.

    Serializes count-check + create + send + post-invalidate so two concurrent
    requests for the same phone cannot both create active verifications and
    then mutually expire each other's row. Single-worker scope only — the
    project runs one uvicorn worker in production, so this is sufficient.
    """
    with _otp_send_master_lock:
        lock = _otp_send_locks.get(phone)
        if lock is None:
            lock = threading.Lock()
            _otp_send_locks[phone] = lock
        return lock


@router.post("/web/sms/send-otp")
def web_send_otp(payload: SendOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if not verify_captcha(payload.captcha_verify_param):
        raise HTTPException(status_code=400, detail="验证码校验未通过")

    with _otp_send_lock_for(phone):
        count = count_verifications_last_hour(phone)
        if count >= settings.aliyun_sms_max_per_phone_per_hour:
            raise HTTPException(status_code=429, detail="发送频率过高，请稍后重试")

        code = generate_otp()
        verification = create_phone_verification(
            phone=phone,
            code=code,
            expires_minutes=settings.otp_expires_minutes,
        )
        try:
            send_otp(phone=phone, code=code)
        except Exception:
            invalidate_verification(verification["id"])
            logger.exception("sms: send failed for phone=%s", phone)
            raise HTTPException(status_code=500, detail="短信发送失败，请稍后重试")

        invalidate_other_verifications_for_phone(phone, verification["id"])
    return {"status": "ok"}


@router.post("/web/sms/verify-otp")
def web_verify_otp(payload: VerifyOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = get_latest_active_verification(phone)
    if verification is None:
        raise HTTPException(status_code=400, detail="验证码不存在或已过期，请重新获取")

    if verification["verify_attempts"] >= 5:
        raise HTTPException(status_code=400, detail="尝试次数过多，请重新获取验证码")

    if verification["code"] != payload.code:
        increment_verify_attempts(verification["id"])
        raise HTTPException(status_code=400, detail="验证码错误")

    result = set_verification_verified(
        verification["id"],
        token_expires_minutes=settings.otp_token_expires_minutes,
    )
    return {"status": "ok", "verified_token": result["verified_token"]}


def _register_platform_user_with_otp_result(
    *,
    phone: str,
    display_name: Optional[str],
    otp_token: str,
    invite_code: Optional[str] = None,
    invalid_otp_detail: str = "注册凭证无效或已过期",
    app_id: str = ZHAOXI_APP_ID,
) -> dict:
    try:
        normalized_phone = normalize_phone(phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    cleaned_invite_code = invite_code.strip() if invite_code else None
    cleaned_invite_code = cleaned_invite_code or None
    try:
        result = register_platform_user_with_referral(
            phone=normalized_phone,
            display_name=display_name,
            invite_code=cleaned_invite_code,
            verified_token=otp_token,
            app_id=app_id,
        )
        return result
    except ValueError as err:
        detail = str(err)
        if detail == "invalid_otp_token":
            raise HTTPException(status_code=400, detail=invalid_otp_detail)
        if detail == "product_membership_disabled":
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=400, detail=detail)


def _register_platform_user_with_otp(
    *,
    phone: str,
    display_name: Optional[str],
    otp_token: str,
    invite_code: Optional[str] = None,
    invalid_otp_detail: str = "注册凭证无效或已过期",
    app_id: str = ZHAOXI_APP_ID,
) -> dict:
    """兼容既有 Web 调用，仅返回注册结果中的真人身份。"""
    return _register_platform_user_with_otp_result(
        phone=phone,
        display_name=display_name,
        otp_token=otp_token,
        invite_code=invite_code,
        invalid_otp_detail=invalid_otp_detail,
        app_id=app_id,
    )["platform_user"]


@router.post("/web/register")
def web_register(payload: WebRegisterRequest) -> dict:
    platform_user = _register_platform_user_with_otp(
        phone=payload.phone,
        display_name=payload.display_name,
        otp_token=payload.otp_token,
        invite_code=payload.invite_code,
    )
    return {
        "status": "ok",
        "platform_user": platform_user,
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user["id"],
        ),
    }


@router.post("/web/register-and-binding-intent")
def web_register_and_binding_intent(
    payload: WebRegisterAndBindingIntentRequest,
) -> dict:
    platform_user = _register_platform_user_with_otp(
        phone=payload.phone,
        display_name=payload.display_name,
        otp_token=payload.otp_token,
        invite_code=payload.invite_code,
    )
    try:
        account_result = get_or_create_default_ai4all_account_for_user(
            platform_user_id=platform_user["id"],
            display_name=None,
            plan="free",
            campaign_code=payload.campaign_code,
        )
        wallet = get_wallet_summary(
            account_id=account_result["account"]["id"],
            ensure_grant=True,
        )
        binding_intent = create_binding_intent(
            platform_user_id=platform_user["id"],
            account_id=account_result["account"]["id"],
            channel=payload.channel or "openclaw-weixin",
        )
        binding_intent = _start_openclaw_qr_for_binding(binding_intent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    session = create_platform_user_session(
        platform_user_id=platform_user["id"], app_id=ZHAOXI_APP_ID, days=7
    )
    return {
        "status": "ok",
        "session_token": session["token"],
        "platform_user": platform_user,
        "account": account_result["account"],
        "profile": account_result["profile"],
        "owner_binding": account_result["owner_binding"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
        "next_step": "scan_qr_and_wait_for_completion",
    }


class WebCampaignVisitRequest(BaseModel):
    campaign_code: str
    visitor_token: Optional[str] = None
    page: Optional[str] = None


@router.post("/web/campaign-visit")
def web_campaign_visit(payload: WebCampaignVisitRequest, request: Request) -> dict:
    """落地页曝光 beacon（漏斗 S0）。无鉴权、fail-open：任何异常都返回 ok，绝不阻塞落地页。

    只接受 campaign_code + 匿名 visitor_token，不接受任何账号/用户标识；只写匿名元数据。
    仅记录命中现存活码的曝光，挡掉任意垃圾 code 放大。见
    campaign_funnel_analytics_technical_design.md §7.3。
    """
    try:
        code = (payload.campaign_code or "").strip()
        if not code:
            return {"status": "ignored"}
        # 轻量防刷：单 IP 限频，超限静默丢弃。
        client_host = request.client.host if request.client else "unknown"
        if not _campaign_visit_rate_limiter.check_rpm(
            f"campaign-visit:{client_host}", _CAMPAIGN_VISIT_RPM, window_seconds=60.0
        ):
            return {"status": "ok"}
        if get_campaign_code(code=code) is None:
            return {"status": "ignored"}
        referrer = request.headers.get("referer")
        user_agent = request.headers.get("user-agent")
        record_campaign_visit(
            campaign_code=code,
            visitor_token=(payload.visitor_token or "")[:64] or None,
            page=(payload.page or "")[:32] or None,
            referrer=referrer[:500] if referrer else None,
            user_agent=user_agent[:500] if user_agent else None,
        )
    except Exception as err:  # fail-open：beacon 不因后端错误影响落地页
        logger.warning("campaign visit beacon failed: %s", err)
    return {"status": "ok"}


@router.post("/web/binding-intents")
def web_create_binding_intent(
    payload: WebCreateBindingIntentRequest,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    try:
        binding_intent = create_binding_intent(
            platform_user_id=principal.platform_user_id,
            account_id=account_result["account"]["id"],
            channel=payload.channel or "openclaw-weixin",
        )
        binding_intent = _start_openclaw_qr_for_binding(binding_intent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
        "next_step": "scan_qr_and_wait_for_completion",
    }


@router.get("/web/binding-intents/{binding_intent_id}")
def web_get_binding_intent(
    binding_intent_id: str,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    binding_intent = get_binding_intent(binding_intent_id=binding_intent_id)
    # 鉴权 + 属主校验：binding_intent 含 qr_data_url / manual_login_command，泄露即可劫持
    # 微信绑定。非属主统一按 404 处理，避免泄露 intent 是否存在。属主访问自己的 QR/登录命令
    # 属正常流程（onboarding 扫码 + home 重绑都依赖），故不脱敏原样返回。
    if (
        binding_intent is None
        or binding_intent.get("platform_user_id") != principal.platform_user_id
    ):
        raise HTTPException(status_code=404, detail="binding_intent not found")
    return {"binding_intent": binding_intent}


class WebLoginRequest(BaseModel):
    verified_token: str
    phone: str
    invite_code: Optional[str] = None
    campaign_code: Optional[str] = None


@router.post("/web/login")
def web_login(payload: WebLoginRequest) -> dict:
    """Exchange a verified OTP token for a session token.

    Handles both new and returning users in one call:
    - Creates or finds platform_user and default account.
    - Creates a 7-day session token.
    - If no active WeChat binding exists, also starts a new binding intent (QR).
    - Returns has_active_binding so the frontend can decide to show QR or go to dashboard.
    """
    platform_user = _register_platform_user_with_otp(
        phone=payload.phone,
        display_name=None,
        otp_token=payload.verified_token,
        invite_code=payload.invite_code,
        invalid_otp_detail="验证凭证无效或已过期",
    )
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=platform_user["id"],
        display_name=None,
        plan="free",
        campaign_code=payload.campaign_code,
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    session = create_platform_user_session(
        platform_user_id=platform_user["id"], app_id=ZHAOXI_APP_ID, days=7
    )
    bindings = list_channel_bindings_for_account(account_id=account_result["account"]["id"])
    has_active_binding = len(bindings) > 0

    # If no binding yet, proactively create a binding intent so the frontend can show QR immediately
    binding_intent = None
    if not has_active_binding:
        try:
            binding_intent = create_binding_intent(
                platform_user_id=platform_user["id"],
                account_id=account_result["account"]["id"],
                channel="openclaw-weixin",
            )
            binding_intent = _start_openclaw_qr_for_binding(binding_intent)
        except Exception as err:
            logger.warning("web_login: failed to create binding intent: %s", err)

    return {
        "status": "ok",
        "session_token": session["token"],
        "platform_user": platform_user,
        "account": account_result["account"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
        "has_active_binding": has_active_binding,
        "binding_intent": binding_intent,
        "binding_mode": "openclaw_gateway_qr",
    }


@router.get("/web/me")
def web_me(principal: SessionPrincipal = Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    platform_user = get_platform_user(platform_user_id=principal.platform_user_id)
    return {
        "status": "ok",
        "platform_user": platform_user,
        "account": account_result["account"],
        "subscription": account_result["subscription"],
        "wallet": wallet,
    }


@router.get("/web/me/referral-code")
def web_me_referral_code(principal: SessionPrincipal = Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    code = get_or_create_personal_referral_code_for_user(
        platform_user_id=principal.platform_user_id,
        app_id=principal.app_id,
    )
    return {
        "status": "ok",
        "referral_code": {
            "code": code["code"],
            "code_type": code["code_type"],
            "status": code["status"],
            "used_count": code["used_count"],
            "invite_param": "invite_code",
        },
    }


@router.get("/web/me/wallet")
def web_me_wallet(principal: SessionPrincipal = Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    wallet = get_wallet_summary(
        account_id=account_result["account"]["id"],
        ensure_grant=True,
    )
    if wallet is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    return {
        "status": "ok",
        "account_id": account_result["account"]["id"],
        "wallet": wallet,
        "ledger": list_wallet_ledger(account_id=account_result["account"]["id"], limit=20),
    }


@router.get("/web/me/bindings")
def web_me_bindings(principal: SessionPrincipal = Depends(_require_session)) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    bindings = list_channel_bindings_for_account(account_id=account_result["account"]["id"])
    return {
        "status": "ok",
        "account_id": account_result["account"]["id"],
        "bindings": bindings,
    }


@router.post("/web/me/unbind")
def web_me_unbind(
    payload: WebUnbindRequest,
    principal: SessionPrincipal = Depends(_require_session),
) -> dict:
    account_result = get_or_create_default_ai4all_account_for_user(
        platform_user_id=principal.platform_user_id,
        display_name=None,
        plan="free",
        app_id=principal.app_id,
    )
    account_id = account_result["account"]["id"]

    bindings_before_unbind = list_channel_bindings_for_account(account_id=account_id)
    openclaw_cleanup = _cleanup_openclaw_weixin_accounts(
        bindings_before_unbind,
        node_id=resolve_node_for_account(account_id),
    )

    if payload.keep_memories:
        stats = unbind_account_channel(account_id=account_id)
    else:
        # 单事务原子完成 unbind + wipe：失败则整体回滚（干净可重试），不再留半成品。
        # P2 后 profile 文件已入库，由 wipe 事务内 delete_account 一并删行（见 stats.profile_files_deleted），
        # 不再有独立的磁盘目录清理。
        stats = unbind_and_wipe_account(account_id=account_id)

    # DB 事务已提交，best-effort 清除 TDAI namespace。
    # 无论 keep_memories 取值：TDAI 是 AI4ALL messages 的派生缓存，随时可从中心 PG 重建，
    # 删除不会丢失权威数据。失败只记 warning，不阻断本次响应。
    _bg = get_background_loop()
    if _bg is not None:
        from app.platform.search.tdai import namespace_wipe as _tdai_namespace_wipe
        _bg.call_soon_threadsafe(
            _bg.create_task,
            _tdai_namespace_wipe(account_id=account_id),
        )
    else:
        logger.warning(
            "tdai namespace_wipe skipped: no background loop account=%s", account_id
        )

    return {
        "status": "ok",
        "keep_memories": payload.keep_memories,
        "account_id": account_id,
        "openclaw_cleanup": openclaw_cleanup,
        "stats": stats,
    }
