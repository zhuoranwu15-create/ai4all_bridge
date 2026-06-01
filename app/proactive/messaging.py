from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.db import (
    claim_pending_outbound_message,
    create_outbound_message,
    insert_outbound_delivery_message,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
)
from app.openclaw_gateway import send_weixin_text
from app.proactive.policy import (
    POLICY_VERSION,
    evaluate_outbound_policy,
    is_quiet_hours,
    normalize_outbound_category,
)


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
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
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

    return create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
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
        metadata=merged_metadata,
    )


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
    except Exception as err:
        failed = mark_outbound_message_failed(
            outbound_message_id=int(claimed["id"]),
            error=str(err),
        )
        if failed is None:
            raise
        return failed

    gateway_message_id = result.get("messageId") if isinstance(result, dict) else None
    sent = mark_outbound_message_sent(
        outbound_message_id=int(claimed["id"]),
        gateway_message_id=str(gateway_message_id) if gateway_message_id else None,
    ) or claimed
    insert_outbound_delivery_message(outbound_message=sent)
    return sent
