from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ResolvedIdentity:
    ai4all_account_id: str
    session_key: str
    channel: str
    channel_account_id: Optional[str]
    sender_id: str
    chat_id: Optional[str]

    @property
    def account_id(self) -> str:
        """Compatibility alias for the current DB/API naming."""
        return self.ai4all_account_id

    def metadata(self) -> Dict[str, Any]:
        return {
            "ai4all_account_id": self.ai4all_account_id,
            "account_id": self.ai4all_account_id,
            "session_key": self.session_key,
            "channel": self.channel,
            "channel_account_id": self.channel_account_id,
            "sender_id": self.sender_id,
            "chat_id": self.chat_id,
        }


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_openclaw_identity(
    *,
    channel: Optional[str],
    session_key: Optional[str],
    channel_account_id: Optional[str],
    sender_id: Optional[str],
    chat_id: Optional[str],
) -> ResolvedIdentity:
    """Resolve OpenClaw payload fields into the AI4ALL business identity.

    OpenClaw's own account_id can represent the provider/bot account and is
    not safe as AI4ALL's business isolation key. For now the stable business
    identity is derived from session_key, with conservative fallbacks for
    debug/mock payloads.
    """
    resolved_session_key = _clean(session_key) or _clean(chat_id) or _clean(sender_id) or "unknown"
    resolved_sender_id = _clean(sender_id) or _clean(chat_id) or resolved_session_key
    return ResolvedIdentity(
        ai4all_account_id=resolved_session_key,
        session_key=resolved_session_key,
        channel=_clean(channel) or "unknown",
        channel_account_id=_clean(channel_account_id),
        sender_id=resolved_sender_id,
        chat_id=_clean(chat_id),
    )
