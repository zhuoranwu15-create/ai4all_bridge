from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import (
    claim_due_content_invitation,
    expire_content_invitations,
    list_channel_bindings_for_account,
    list_due_content_invitations,
    mark_content_invitation_invited,
    mark_content_invitation_rejected_by_policy,
    release_content_invitation_claim,
)
from app.proactive.messaging import send_proactive_text


def format_content_invitation_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _select_route(account_id: str) -> Optional[Dict[str, Any]]:
    for binding in list_channel_bindings_for_account(account_id=account_id):
        to_user_id = _clean_text(binding.get("chat_id"))
        channel_account_id = _clean_text(binding.get("channel_account_id"))
        if not to_user_id or not channel_account_id:
            continue
        return {
            "channel_binding_id": binding["id"],
            "channel": binding["channel"],
            "channel_account_id": channel_account_id,
            "to_user_id": to_user_id,
            "session_key": binding.get("session_key"),
        }
    return None


def dispatch_content_invitation(
    *,
    invitation_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    current = now or datetime.now()
    current_text = format_content_invitation_time(current)
    claimed = claim_due_content_invitation(
        invitation_id=invitation_id,
        now=current_text,
    )
    if claimed is None:
        return {
            "status": "skipped",
            "reason": "not_due_or_already_claimed",
            "content_invitation_id": invitation_id,
        }

    route = _select_route(claimed["account_id"])
    if route is None:
        invitation = mark_content_invitation_rejected_by_policy(
            invitation_id=claimed["id"],
            outbound_message_id=None,
            policy_reason="missing_channel_route",
        )
        return {
            "status": "rejected_by_policy",
            "reason": "missing_channel_route",
            "content_invitation": invitation,
        }

    expires_at = claimed.get("expires_at")
    if not expires_at:
        expire_hours = int(getattr(settings, "content_invitation_expire_hours", 24) or 24)
        expires_at = format_content_invitation_time(
            current + timedelta(hours=max(expire_hours, 1))
        )

    outbound = send_proactive_text(
        account_id=claimed["account_id"],
        channel=route["channel"],
        channel_account_id=route.get("channel_account_id"),
        to_user_id=route["to_user_id"],
        session_key=route.get("session_key"),
        source="content_invitation",
        text=claimed["invitation_text"],
        idempotency_key=f"content-invitation-{claimed['id']}",
        now=current,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category="content_invitation",
        metadata={
            "content_invitation_id": claimed["id"],
            "topic": claimed["topic"],
            "title_count": len(claimed.get("title_items") or []),
            "channel_binding_id": route.get("channel_binding_id"),
        },
    )

    outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
    outbound_status = outbound.get("status")
    if outbound_status == "sent":
        invitation = mark_content_invitation_invited(
            invitation_id=claimed["id"],
            outbound_message_id=outbound_id,
            invited_at=current_text,
            expires_at=expires_at,
        )
        return {
            "status": "invited",
            "content_invitation": invitation,
            "outbound_message": outbound,
        }

    if outbound_status == "pending":
        # outbound was created but claim lost a race — transient, not a real rejection.
        # Release the invitation claim so the scheduler can retry it.
        release_content_invitation_claim(invitation_id=claimed["id"])
        return {
            "status": "skipped",
            "reason": "outbound_claim_race",
            "content_invitation_id": claimed["id"],
            "outbound_message": outbound,
        }

    invitation = mark_content_invitation_rejected_by_policy(
        invitation_id=claimed["id"],
        outbound_message_id=outbound_id,
        policy_reason=outbound.get("error") or f"outbound_status:{outbound_status}",
    )
    return {
        "status": "rejected_by_policy",
        "reason": outbound.get("error"),
        "content_invitation": invitation,
        "outbound_message": outbound,
    }


def dispatch_due_content_invitations(
    *,
    now: Optional[datetime] = None,
    limit: int = 20,
    bypass_quiet_hours: bool = False,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    due = list_due_content_invitations(
        now=format_content_invitation_time(current),
        limit=limit,
    )
    results: List[Dict[str, Any]] = []
    for item in due:
        results.append(
            dispatch_content_invitation(
                invitation_id=item["id"],
                now=current,
                bypass_quiet_hours=bypass_quiet_hours,
            )
        )
    return results


def expire_stale_content_invitations(
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    current = now or datetime.now()
    return expire_content_invitations(
        now=format_content_invitation_time(current),
        limit=limit,
    )
