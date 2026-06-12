import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    claim_pending_outbound_message,
    create_outbound_message,
    insert_outbound_delivery_message,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
    resolve_node_for_account,
    update_outbound_message_metadata,
)
from app.moderation.sensitive_words import check_sync_guard
from app.moderation.service import create_sync_block_task, enqueue_outbound_for_moderation
from app.openclaw_gateway import OpenClawRateLimited, send_weixin_text
from app.proactive.policy import (
    POLICY_VERSION,
    evaluate_outbound_policy,
    is_quiet_hours,
    normalize_outbound_category,
)


logger = logging.getLogger("ai4all.proactive.messaging")


def enqueue_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    node_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    # 多机出站:解析归属节点写入 outbound_messages.node_id,供节点按 node 认领。
    # 显式 node_id 优先;否则查账号归属;再回落 default_node_id(迁移期=aliyun1)。
    # standalone 下 default_node_id 通常为空 → node_id=None,send_proactive_text 仍按 id 直发,行为不变。
    effective_node_id = (
        node_id
        or resolve_node_for_account(account_id)
        or (getattr(settings, "default_node_id", "") or None)
    )
    merged_metadata = {
        **(metadata or {}),
    }
    if bypass_quiet_hours:
        merged_metadata["bypass_quiet_hours"] = True

    category = normalize_outbound_category(
        source=source,
        product_category=product_category,
    )
    decision = evaluate_outbound_policy(
        account_id=account_id,
        category=category,
        source=source,
        scheduled_at=scheduled_at,
        now=current,
        metadata=merged_metadata,
    )
    merged_metadata.update(decision.metadata)
    merged_metadata["product_category"] = category.value
    merged_metadata["policy_version"] = POLICY_VERSION
    merged_metadata["policy_decision"] = "allowed" if decision.allowed else "blocked"
    if decision.reason:
        merged_metadata["policy_reason"] = decision.reason
        merged_metadata["policy_error"] = decision.reason
    if decision.counts:
        merged_metadata.update(decision.counts)
    if decision.next_allowed_at:
        merged_metadata["next_allowed_at"] = decision.next_allowed_at

    effective_idempotency_key = idempotency_key or f"proactive-{account_id}-{uuid.uuid4().hex}"
    if decision.allowed:
        sync_decision = check_sync_guard(
            account_id=account_id,
            text=text,
            direction="outbound",
            content_kind="text",
            source_type="outbound_message",
            source_id=effective_idempotency_key,
        )
        if not sync_decision.allowed:
            merged_metadata.update(
                {
                    "moderation_sync_blocked": True,
                    "moderation_risk_level": sync_decision.level,
                    "moderation_categories": sync_decision.categories,
                    "policy_reason": "moderation_sync_blocked",
                    "policy_error": "moderation_sync_blocked",
                }
            )
            outbound = create_outbound_message(
                account_id=account_id,
                channel=channel,
                channel_account_id=channel_account_id,
                to_user_id=to_user_id,
                session_key=session_key,
                source=source,
                text=str(getattr(settings, "moderation_blocked_placeholder", "") or "[blocked by moderation]"),
                idempotency_key=effective_idempotency_key,
                quota_date=decision.quota_date,
                status="cancelled",
                error="moderation_sync_blocked",
                product_category=category.value,
                policy_version=POLICY_VERSION,
                policy_reason="moderation_sync_blocked",
                scheduled_at=(
                    scheduled_at.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                    if scheduled_at
                    else None
                ),
                node_id=effective_node_id,
                metadata=merged_metadata,
            )
            try:
                blocked_task = create_sync_block_task(
                    account_id=account_id,
                    session_id=None,
                    source_type="outbound_message",
                    source_id=str(outbound["id"]),
                    direction="outbound",
                    content_kind="text",
                    text=text,
                    decision=sync_decision,
                    outbound_message_id=int(outbound["id"]),
                    metadata={
                        "source": source,
                        "product_category": category.value,
                        "to_user_id": to_user_id,
                        "idempotency_key": effective_idempotency_key,
                    },
                )
                updated = update_outbound_message_metadata(
                    outbound_message_id=int(outbound["id"]),
                    metadata_patch={"moderation_task_id": blocked_task.get("id")},
                )
                return updated or outbound
            except Exception as err:
                logger.exception(
                    "proactive sync moderation block task failed account=%s outbound_id=%s error=%s",
                    account_id,
                    outbound.get("id"),
                    err,
                )
                return outbound

    outbound = create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=effective_idempotency_key,
        quota_date=decision.quota_date,
        status=decision.status,
        error=decision.reason,
        product_category=category.value,
        policy_version=POLICY_VERSION,
        policy_reason=decision.reason,
        scheduled_at=(
            scheduled_at.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            if scheduled_at
            else None
        ),
        node_id=effective_node_id,
        metadata=merged_metadata,
    )
    if outbound["status"] == "pending":
        try:
            enqueue_outbound_for_moderation(outbound_message_id=int(outbound["id"]))
        except Exception as err:
            logger.exception(
                "proactive moderation enqueue failed account=%s outbound_id=%s error=%s",
                account_id,
                outbound.get("id"),
                err,
            )
    return outbound


def send_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outbound = enqueue_proactive_text(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        now=now,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category=product_category,
        scheduled_at=scheduled_at,
        metadata=metadata,
    )
    if outbound["status"] != "pending":
        return outbound

    claimed = claim_pending_outbound_message(outbound_message_id=int(outbound["id"]))
    if claimed is None:
        return outbound

    # 限速（ret=-2 / rate limited）按账号退避重试；其它异常立即落 failed。
    # 重试复用同一 idempotency_key，网关侧幂等，不会重复投递。本路径为后台主动消息，
    # 阻塞数秒可接受，不影响用户同步回复。
    max_retries = max(0, int(getattr(settings, "proactive_send_rate_limit_max_retries", 2) or 0))
    backoff_seconds = float(getattr(settings, "proactive_send_rate_limit_backoff_seconds", 3.0) or 0.0)
    result: Optional[Dict[str, Any]] = None
    for attempt in range(max_retries + 1):
        try:
            result = send_weixin_text(
                to_user_id=to_user_id,
                text=text,
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=channel_account_id,
                idempotency_key=claimed["idempotency_key"],
                session_key=session_key,
                channel=channel,
            )
            break
        except OpenClawRateLimited as err:
            # 还有重试次数则再发；退避秒数 > 0 时先线性退避（backoff * 次数）。
            if attempt < max_retries:
                if backoff_seconds > 0:
                    time.sleep(backoff_seconds * (attempt + 1))
                continue
            failed = mark_outbound_message_failed(
                outbound_message_id=int(claimed["id"]),
                error=f"rate_limited: {err}",
            )
            if failed is None:
                raise
            return failed
        except Exception as err:
            failed = mark_outbound_message_failed(
                outbound_message_id=int(claimed["id"]),
                error=str(err),
            )
            if failed is None:
                raise
            return failed
    assert result is not None  # 循环要么 break 成功，要么在 except 内 return

    gateway_message_id = result.get("messageId") if isinstance(result, dict) else None
    sent = mark_outbound_message_sent(
        outbound_message_id=int(claimed["id"]),
        gateway_message_id=str(gateway_message_id) if gateway_message_id else None,
    ) or claimed
    insert_outbound_delivery_message(outbound_message=sent)
    return sent
