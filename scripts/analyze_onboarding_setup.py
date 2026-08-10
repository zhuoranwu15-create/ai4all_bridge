"""
Analyze active-account onboarding setup from context files and same-day messages.

Read-only diagnostic:

    .venv/bin/python scripts/analyze_onboarding_setup.py
    .venv/bin/python scripts/analyze_onboarding_setup.py --start-date 2026-06-01 --json
    .venv/bin/python scripts/analyze_onboarding_setup.py --account aid_123456789 --details
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Optional

# Allow running from repo root or scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.agent_runtime.persistence import profile_storage
    from app.config import settings
    from app.db import connect
    from app.db._backend import Connection
    from app.products.zhaoxi.infrastructure.profiles import _safe_account_dir_name
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行：\n"
        "  .venv/bin/python scripts/analyze_onboarding_setup.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


_USER_NAME_RE = re.compile(r"(?m)^[ \t]*-?[ \t]*用户称呼[:：][ \t]*(.+?)\s*$")
_AI_NAME_RE = re.compile(r"(?m)^[ \t]*-?[ \t]*AI 名字[:：][ \t]*(.+?)\s*$")
_ANY_IDENTITY_NAME_RE = re.compile(r"(?m)^[ \t]*-?[ \t]*你的名字是\s*(.+?)(?:，|,|。|\s*$)")
_NO_NAME_IDENTITY_MARKER = "你还没有名字"

_PERSONA_MARKERS = {
    "xiaotaiyang": ("小太阳", "清晨不由分说拉开窗帘", "明亮、主动、能量往外扑"),
    "xiaoyueya": ("小月牙", "安静发着微光", "月夜"),
    "ju": ("橘", "慵懒、傲娇", "像一只趴在窗台上晒太阳的猫"),
}
_CUSTOM_PERSONA_MARKER = "用户对你的期待描述："
_BLANK_SOUL_MARKERS = ("你的底色是温柔、风趣、有自主性", "用户选择了你，不是因为你能做什么")


@dataclass(frozen=True)
class AccountStats:
    account_id: str
    channel: Optional[str]
    status: str
    is_debug: bool
    onboarding_state: str
    registered_at: str
    registered_date: str
    profile_dir: str
    has_user_name: bool
    user_name: Optional[str]
    has_ai_name: bool
    ai_name: Optional[str]
    identity_has_any_name: bool
    identity_any_name: Optional[str]
    has_persona: bool
    persona: str
    registration_day_messages: int
    registration_day_messages_by_role: Dict[str, int]
    registration_day_messages_by_direction: Dict[str, int]
    first_message_at: Optional[str]
    last_message_at: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "account_id": self.account_id,
            "channel": self.channel,
            "status": self.status,
            "is_debug": self.is_debug,
            "onboarding_state": self.onboarding_state,
            "registered_at": self.registered_at,
            "registered_date": self.registered_date,
            "profile_dir": self.profile_dir,
            "has_user_name": self.has_user_name,
            "user_name": self.user_name,
            "has_ai_name": self.has_ai_name,
            "ai_name": self.ai_name,
            "identity_has_any_name": self.identity_has_any_name,
            "identity_any_name": self.identity_any_name,
            "has_persona": self.has_persona,
            "persona": self.persona,
            "registration_day_messages": self.registration_day_messages,
            "registration_day_messages_by_role": self.registration_day_messages_by_role,
            "registration_day_messages_by_direction": self.registration_day_messages_by_direction,
            "first_message_at": self.first_message_at,
            "last_message_at": self.last_message_at,
        }


def _profiles_root(raw: Optional[str]) -> Path:
    """Resolve the account context-file root without creating directories."""
    path = Path(raw or settings.user_profiles_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _read_text(path: Path) -> str:
    """Read a UTF-8 text file; missing/unreadable files count as empty."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _extract_first(pattern: re.Pattern[str], text: str) -> Optional[str]:
    """Extract the first non-empty regex group from text."""
    match = pattern.search(text or "")
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def classify_persona(soul_text: str) -> str:
    """Classify SOUL.md into an onboarding persona bucket."""
    text = soul_text or ""
    if not text.strip():
        return "missing"
    if _CUSTOM_PERSONA_MARKER in text:
        return "custom"
    for persona, markers in _PERSONA_MARKERS.items():
        if any(marker in text for marker in markers):
            return persona
    if any(marker in text for marker in _BLANK_SOUL_MARKERS):
        return "blank"
    return "custom_or_legacy"


def _validate_date(value: Optional[str], arg_name: str) -> Optional[str]:
    """Validate YYYY-MM-DD CLI date strings."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"{arg_name} must be YYYY-MM-DD, got: {value}") from exc


def fetch_accounts(
    conn: Connection,
    *,
    account_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    include_inactive: bool,
    include_debug: bool,
) -> List[Dict[str, Any]]:
    """Fetch account cohort rows with account-isolated filters."""
    clauses = ["1 = 1"]
    params: List[Any] = []
    if account_id:
        clauses.append("id = ?")
        params.append(account_id)
    if not include_inactive:
        clauses.append("status = 'active'")
    if not include_debug:
        clauses.append("COALESCE(is_debug, 0) = 0")
    if start_date:
        clauses.append("substr(created_at, 1, 10) >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("substr(created_at, 1, 10) <= ?")
        params.append(end_date)

    sql = f"""
        SELECT id, channel, status, COALESCE(is_debug, 0) AS is_debug,
               onboarding_state, created_at
        FROM accounts
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at ASC, id ASC
    """
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fetch_registration_day_message_stats(
    conn: Connection,
    *,
    account_id: str,
    registered_date: str,
) -> Dict[str, Any]:
    """Count all message rows for one account on its registration date."""
    rows = conn.execute(
        """
        SELECT role, direction, COUNT(*) AS count
        FROM messages
        WHERE account_id = ?
          AND substr(created_at, 1, 10) = ?
        GROUP BY role, direction
        """,
        (account_id, registered_date),
    ).fetchall()
    bounds = conn.execute(
        """
        SELECT MIN(created_at) AS first_message_at, MAX(created_at) AS last_message_at
        FROM messages
        WHERE account_id = ?
          AND substr(created_at, 1, 10) = ?
        """,
        (account_id, registered_date),
    ).fetchone()

    by_role: Dict[str, int] = {}
    by_direction: Dict[str, int] = {}
    total = 0
    for row in rows:
        count = int(row["count"] or 0)
        total += count
        role = str(row["role"] or "unknown")
        direction = str(row["direction"] or "unknown")
        by_role[role] = by_role.get(role, 0) + count
        by_direction[direction] = by_direction.get(direction, 0) + count

    return {
        "total": total,
        "by_role": dict(sorted(by_role.items())),
        "by_direction": dict(sorted(by_direction.items())),
        "first_message_at": bounds["first_message_at"] if bounds else None,
        "last_message_at": bounds["last_message_at"] if bounds else None,
    }


def analyze_account(
    conn: Connection,
    *,
    account: Dict[str, Any],
    profiles_root: Path,
) -> AccountStats:
    """Analyze one account using only account-scoped DB rows and profile files."""
    account_id = str(account["id"])
    registered_at = str(account["created_at"] or "")
    registered_date = registered_at[:10]
    profile_dir = profiles_root / _safe_account_dir_name(account_id)

    # P2 后从 storage 读（account_profile_files），profile_dir 仅保留为元数据展示字段。
    soul_text = (profile_storage.read_file(account_id, "SOUL.md") or "").strip()
    identity_text = (profile_storage.read_file(account_id, "IDENTITY.md") or "").strip()
    user_text = (profile_storage.read_file(account_id, "USER.md") or "").strip()

    user_name = _extract_first(_USER_NAME_RE, user_text)
    ai_name = _extract_first(_AI_NAME_RE, identity_text)
    identity_any_name = _extract_first(_ANY_IDENTITY_NAME_RE, identity_text)
    identity_has_any_name = bool(ai_name or identity_any_name) and _NO_NAME_IDENTITY_MARKER not in identity_text

    persona = classify_persona(soul_text)
    has_persona = persona not in {"missing", "blank"}
    message_stats = fetch_registration_day_message_stats(
        conn,
        account_id=account_id,
        registered_date=registered_date,
    )

    return AccountStats(
        account_id=account_id,
        channel=account.get("channel"),
        status=str(account.get("status") or ""),
        is_debug=bool(account.get("is_debug")),
        onboarding_state=str(account.get("onboarding_state") or "pending"),
        registered_at=registered_at,
        registered_date=registered_date,
        profile_dir=str(profile_dir),
        has_user_name=bool(user_name),
        user_name=user_name,
        has_ai_name=bool(ai_name),
        ai_name=ai_name,
        identity_has_any_name=identity_has_any_name,
        identity_any_name=ai_name or identity_any_name,
        has_persona=has_persona,
        persona=persona,
        registration_day_messages=int(message_stats["total"]),
        registration_day_messages_by_role=message_stats["by_role"],
        registration_day_messages_by_direction=message_stats["by_direction"],
        first_message_at=message_stats["first_message_at"],
        last_message_at=message_stats["last_message_at"],
    )


def _ratio(count: int, total: int) -> Optional[float]:
    """Return a rounded ratio or None when the denominator is empty."""
    if total <= 0:
        return None
    return round(count / total, 4)


def summarize(items: List[AccountStats]) -> Dict[str, Any]:
    """Build aggregate onboarding setup metrics."""
    total = len(items)
    user_name_count = sum(1 for item in items if item.has_user_name)
    ai_name_count = sum(1 for item in items if item.has_ai_name)
    identity_any_name_count = sum(1 for item in items if item.identity_has_any_name)
    persona_count = sum(1 for item in items if item.has_persona)
    message_total = sum(item.registration_day_messages for item in items)

    persona_distribution: Dict[str, int] = {}
    onboarding_state_distribution: Dict[str, int] = {}
    for item in items:
        persona_distribution[item.persona] = persona_distribution.get(item.persona, 0) + 1
        onboarding_state_distribution[item.onboarding_state] = (
            onboarding_state_distribution.get(item.onboarding_state, 0) + 1
        )

    return {
        "account_count": total,
        "user_name_set_count": user_name_count,
        "user_name_set_ratio": _ratio(user_name_count, total),
        "ai_name_set_count": ai_name_count,
        "ai_name_set_ratio": _ratio(ai_name_count, total),
        "identity_any_name_count": identity_any_name_count,
        "identity_any_name_ratio": _ratio(identity_any_name_count, total),
        "persona_set_count": persona_count,
        "persona_set_ratio": _ratio(persona_count, total),
        "registration_day_message_total": message_total,
        "registration_day_message_avg": round(message_total / total, 2) if total else None,
        "persona_distribution": dict(sorted(persona_distribution.items())),
        "onboarding_state_distribution": dict(sorted(onboarding_state_distribution.items())),
    }


def _percent(value: Optional[float]) -> str:
    """Format ratios for the human-readable table."""
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def print_text_report(
    *,
    summary: Dict[str, Any],
    items: Iterable[AccountStats],
    details: bool,
) -> None:
    """Print a compact terminal report."""
    print("Onboarding setup analysis")
    print(f"accounts: {summary['account_count']}")
    print(
        "user nickname set: "
        f"{summary['user_name_set_count']} ({_percent(summary['user_name_set_ratio'])})"
    )
    print(
        "AI nickname set: "
        f"{summary['ai_name_set_count']} ({_percent(summary['ai_name_set_ratio'])})"
    )
    print(
        "AI persona set: "
        f"{summary['persona_set_count']} ({_percent(summary['persona_set_ratio'])})"
    )
    print(
        "registration-day messages: "
        f"{summary['registration_day_message_total']} total, "
        f"{summary['registration_day_message_avg']} avg/account"
    )
    print(f"persona distribution: {json.dumps(summary['persona_distribution'], ensure_ascii=False)}")
    print(
        "onboarding states: "
        f"{json.dumps(summary['onboarding_state_distribution'], ensure_ascii=False)}"
    )
    print(
        "note: AI nickname ratio is strict `AI 名字：` from onboarding writes; "
        "`identity_any_name_ratio` is available in --json for legacy/default identity names."
    )

    if not details:
        return

    print()
    print("account_id | registered_date | user_name | ai_name | persona | day_messages | roles")
    for item in items:
        print(
            " | ".join(
                [
                    item.account_id,
                    item.registered_date,
                    "Y" if item.has_user_name else "N",
                    "Y" if item.has_ai_name else "N",
                    item.persona,
                    str(item.registration_day_messages),
                    json.dumps(item.registration_day_messages_by_role, ensure_ascii=False),
                ]
            )
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line flags."""
    parser = argparse.ArgumentParser(
        description=(
            "Analyze active accounts' onboarding setup from SOUL.md, IDENTITY.md, "
            "USER.md, and registration-day messages."
        )
    )
    parser.add_argument("--profiles-dir", help="user_profiles root; defaults to settings.user_profiles_dir")
    parser.add_argument("--account", help="Analyze one account_id")
    parser.add_argument("--start-date", help="Registration date lower bound, YYYY-MM-DD")
    parser.add_argument("--end-date", help="Registration date upper bound, YYYY-MM-DD")
    parser.add_argument("--include-inactive", action="store_true", help="Include non-active accounts")
    parser.add_argument("--include-debug", action="store_true", help="Include accounts where is_debug=1")
    parser.add_argument("--details", action="store_true", help="Print per-account rows")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    start_date = _validate_date(args.start_date, "--start-date")
    end_date = _validate_date(args.end_date, "--end-date")
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must be earlier than or equal to --end-date")

    profiles_root = _profiles_root(args.profiles_dir)
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        accounts = fetch_accounts(
            conn,
            account_id=args.account,
            start_date=start_date,
            end_date=end_date,
            include_inactive=args.include_inactive,
            include_debug=args.include_debug,
        )
        items = [
            analyze_account(conn, account=account, profiles_root=profiles_root)
            for account in accounts
        ]

    output = {
        "filters": {
            "database": "postgresql",
            "profiles_dir": str(profiles_root),
            "account": args.account,
            "start_date": start_date,
            "end_date": end_date,
            "include_inactive": bool(args.include_inactive),
            "include_debug": bool(args.include_debug),
        },
        "summary": summarize(items),
        "accounts": [item.as_dict() for item in items],
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print_text_report(summary=output["summary"], items=items, details=bool(args.details))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
