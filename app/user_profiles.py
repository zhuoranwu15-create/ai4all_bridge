import re
from pathlib import Path

from app.config import settings


DEFAULT_USER_PROFILE = """# User Profile

## Soul
你是这个微信账号的个人 AI 陪伴与生活助理。回应要自然、温和、简洁，优先提供情绪陪伴、日常建议和生活协助。

## User Preferences
- 暂无

## Long-term Memory
- 暂无
"""


def _safe_account_dir_name(account_id: str) -> str:
    value = account_id.strip() or "default"
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value[:160] or "default"


def user_profile_path(account_id: str) -> Path:
    return Path(settings.user_profiles_dir) / _safe_account_dir_name(account_id) / "user_profile.md"


def ensure_user_profile(account_id: str) -> Path:
    path = user_profile_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_USER_PROFILE, encoding="utf-8")
    return path


def read_user_profile(account_id: str) -> str:
    path = ensure_user_profile(account_id)
    return path.read_text(encoding="utf-8").strip()


def read_daily_notes(account_id: str, today: str) -> str:
    """Read today's and yesterday's memory notes, combined. Returns empty string if neither exists."""
    from datetime import date, timedelta

    today_date = date.fromisoformat(today)
    yesterday_str = (today_date - timedelta(days=1)).isoformat()

    base = Path(settings.user_profiles_dir) / _safe_account_dir_name(account_id) / "memory"
    parts = []
    for date_str in [today, yesterday_str]:
        p = base / f"{date_str}.md"
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)
