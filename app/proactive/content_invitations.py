from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db import expire_content_invitations
from app.time_utils import beijing_naive_now


def format_content_invitation_time(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def expire_stale_content_invitations(
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    current = now or beijing_naive_now()
    return expire_content_invitations(
        now=format_content_invitation_time(current),
        limit=limit,
    )
