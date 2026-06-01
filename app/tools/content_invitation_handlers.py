from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.config import settings
from app.db import (
    create_content_invitation,
    get_active_content_invitation,
    get_content_invitation,
    mark_content_invitation_feedback,
    mark_content_invitation_titles_sent,
    upsert_content_invitation_preference,
)

if TYPE_CHECKING:
    from app.turn_context import TurnContext


FEEDBACK_TYPES = {"decline", "block_topic", "less_like_this", "more_like_this"}


def _format_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_url(text: str) -> bool:
    lowered = text.lower()
    return "http://" in lowered or "https://" in lowered or "www." in lowered


def _sanitize_title_items(raw_items: Any, *, max_items: int = 10) -> List[Dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in raw_items[:max_items]:
        if isinstance(item, dict):
            title = _clean_text(item.get("title"))
            if not title:
                continue
            items.append(
                {
                    "title": title[:200],
                    "source_name": _clean_text(item.get("source_name")),
                    "url": _clean_text(item.get("url")),
                    "published_at": _clean_text(item.get("published_at")),
                    "retrieved_at": _format_time(datetime.now()),
                }
            )
        else:
            title = _clean_text(item)
            if title:
                items.append({"title": title[:200], "retrieved_at": _format_time(datetime.now())})
    return items


def _active_invitation_for_args(args: dict, ctx: "TurnContext") -> Optional[Dict[str, Any]]:
    invitation_id = _clean_text(args.get("invitation_id"))
    if invitation_id:
        invitation = get_content_invitation(invitation_id=invitation_id)
        if invitation and invitation["account_id"] == ctx.account_id:
            return invitation
        return None
    return get_active_content_invitation(
        account_id=ctx.account_id,
        now=_format_time(datetime.now()),
    )


def handle_create_content_invitation_candidate(args: dict, ctx: "TurnContext") -> dict:
    topic = _clean_text(args.get("topic"))
    invitation_text = _clean_text(args.get("invitation_text"))
    title_items = _sanitize_title_items(args.get("title_items"), max_items=10)
    reason = _clean_text(args.get("reason"))

    if not topic:
        return {"error": "topic 不能为空"}
    if not invitation_text:
        return {"error": "invitation_text 不能为空"}
    if _has_url(invitation_text):
        return {"error": "主动邀请文本不能包含 URL"}
    if len(title_items) < 3:
        return {"error": "title_items 至少需要 3 条标题"}

    current = datetime.now()
    expire_hours = int(getattr(settings, "content_invitation_expire_hours", 24) or 24)
    invitation = create_content_invitation(
        account_id=ctx.account_id,
        topic=topic,
        invitation_text=invitation_text[:240],
        title_items=title_items,
        scheduled_at=_format_time(current),
        expires_at=_format_time(current + timedelta(hours=max(expire_hours, 1))),
        metadata={
            "source": "tool_use",
            "source_message_id": ctx.message_id,
            "reason": reason,
        },
    )
    return {
        "status": "created",
        "invitation_id": invitation["id"],
        "topic": invitation["topic"],
        "scheduled_at": invitation["scheduled_at"],
        "title_count": len(invitation["title_items"]),
    }


def handle_skip_content_invitation(args: dict, ctx: "TurnContext") -> dict:
    return {
        "status": "skipped",
        "reason": _clean_text(args.get("reason")) or "not_suitable",
        "account_id": ctx.account_id,
    }


def handle_send_content_invitation_titles(
    args: dict,
    ctx: "TurnContext",
    *,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    invitation = _active_invitation_for_args(args, ctx)
    if invitation is None:
        return {"error": "内容邀请不存在或无权操作"}
    if invitation["status"] == "titles_sent":
        return {"status": "already_sent", "invitation_id": invitation["id"], "titles": []}
    if invitation["status"] != "invited":
        return {"error": f"该内容邀请状态为 {invitation['status']}，无法发送标题"}
    expires_at = _clean_text(invitation.get("expires_at"))
    now = datetime.now()
    if expires_at and datetime.fromisoformat(expires_at.replace(" ", "T")) <= now:
        return {"error": "内容邀请已过期"}

    try:
        max_titles = int(args.get("max_titles") or 10)
    except (TypeError, ValueError):
        max_titles = 10
    max_titles = max(1, min(max_titles, 10))
    titles = [
        {"title": str(item.get("title") or "").strip()[:200]}
        for item in (invitation.get("title_items") or [])[:max_titles]
        if str(item.get("title") or "").strip()
    ]
    updated = mark_content_invitation_titles_sent(
        invitation_id=invitation["id"],
        trigger_message_id=ctx.message_id,
        tool_invocation_id=tool_invocation_id,
        responded_at=_format_time(now),
    )
    if updated is None or updated["status"] != "titles_sent":
        return {"error": "内容邀请状态更新失败"}
    return {
        "status": "titles_sent",
        "invitation_id": invitation["id"],
        "topic": invitation["topic"],
        "titles": titles,
    }


def handle_record_content_invitation_feedback(
    args: dict,
    ctx: "TurnContext",
    *,
    tool_invocation_id: Optional[int] = None,
) -> dict:
    feedback_type = _clean_text(args.get("feedback_type"))
    if feedback_type not in FEEDBACK_TYPES:
        return {"error": "feedback_type 无效"}

    invitation = _active_invitation_for_args(args, ctx)
    topic = _clean_text(args.get("topic")) or (invitation or {}).get("topic")
    if not topic:
        return {"error": "topic 不能为空"}
    note = _clean_text(args.get("note"))
    now = datetime.now()

    invitation_status = "declined" if feedback_type in {"decline", "block_topic", "less_like_this"} else "accepted"
    if invitation is not None:
        mark_content_invitation_feedback(
            invitation_id=invitation["id"],
            status=invitation_status,
            trigger_message_id=ctx.message_id,
            tool_invocation_id=tool_invocation_id,
            responded_at=_format_time(now),
            metadata={"feedback_type": feedback_type, "feedback_note": note},
        )

    preference_status = "allowed"
    cooldown_until = None
    if feedback_type == "block_topic":
        preference_status = "blocked"
    elif feedback_type in {"decline", "less_like_this"}:
        preference_status = "cooled_down"
        cooldown_days = int(getattr(settings, "content_invitation_rejection_cooldown_days", 30) or 30)
        cooldown_until = _format_time(now + timedelta(days=max(cooldown_days, 1)))

    preference = upsert_content_invitation_preference(
        account_id=ctx.account_id,
        topic=topic,
        status=preference_status,
        cooldown_until=cooldown_until,
        metadata={
            "feedback_type": feedback_type,
            "note": note,
            "source_message_id": ctx.message_id,
        },
    )
    return {
        "status": "recorded",
        "feedback_type": feedback_type,
        "topic": topic,
        "preference": {
            "status": preference["status"],
            "cooldown_until": preference.get("cooldown_until"),
            "feedback_count": preference["feedback_count"],
        },
    }
