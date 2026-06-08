"""
Analyze new-user D1 retention from account registration dates and user messages.

Read-only diagnostic:

    .venv/bin/python scripts/analyze_next_day_retention.py
    .venv/bin/python scripts/analyze_next_day_retention.py --start-date 2026-06-01 --json
    .venv/bin/python scripts/analyze_next_day_retention.py --active-only --details
    .venv/bin/python scripts/analyze_next_day_retention.py --include-incomplete-d1
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, Iterable, List, Optional

# Allow running from repo root or scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.config import settings
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行：\n"
        "  .venv/bin/python scripts/analyze_next_day_retention.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


@dataclass(frozen=True)
class AccountRetention:
    account_id: str
    channel: Optional[str]
    status: str
    is_debug: bool
    onboarding_state: str
    registered_at: str
    registered_date: str
    d1_date: str
    d0_user_messages: int
    d1_user_messages: int
    retained_d1: bool
    d1_first_user_message_at: Optional[str]
    d1_last_user_message_at: Optional[str]

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
            "d1_date": self.d1_date,
            "d0_user_messages": self.d0_user_messages,
            "d1_user_messages": self.d1_user_messages,
            "retained_d1": self.retained_d1,
            "d1_first_user_message_at": self.d1_first_user_message_at,
            "d1_last_user_message_at": self.d1_last_user_message_at,
        }


def _db_path(raw: Optional[str]) -> Path:
    """Resolve the SQLite database path without creating files."""
    path = Path(raw or settings.database_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _parse_date(value: str) -> datetime:
    """Parse a YYYY-MM-DD date string."""
    return datetime.strptime(value, "%Y-%m-%d")


def _validate_date(value: Optional[str], arg_name: str) -> Optional[str]:
    """Validate YYYY-MM-DD CLI date strings."""
    if not value:
        return None
    try:
        return _parse_date(value).date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"{arg_name} must be YYYY-MM-DD, got: {value}") from exc


def _d1_date(registered_date: str) -> str:
    """Return the next local calendar date after registration."""
    return (_parse_date(registered_date) + timedelta(days=1)).date().isoformat()


def _eligible_end_date(as_of_date: str) -> str:
    """Return the latest registration date whose D1 has fully elapsed."""
    return (_parse_date(as_of_date) - timedelta(days=2)).date().isoformat()


def fetch_accounts(
    conn: sqlite3.Connection,
    *,
    account_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    as_of_date: str,
    include_incomplete_d1: bool,
    active_only: bool,
    include_debug: bool,
) -> List[Dict[str, Any]]:
    """Fetch account cohort rows with account-isolated filters."""
    clauses = ["1 = 1"]
    params: List[Any] = []
    if account_id:
        clauses.append("id = ?")
        params.append(account_id)
    if active_only:
        clauses.append("status = 'active'")
    if not include_debug:
        clauses.append("COALESCE(is_debug, 0) = 0")
    if start_date:
        clauses.append("substr(created_at, 1, 10) >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("substr(created_at, 1, 10) <= ?")
        params.append(end_date)
    if not include_incomplete_d1:
        clauses.append("substr(created_at, 1, 10) <= ?")
        params.append(_eligible_end_date(as_of_date))

    sql = f"""
        SELECT id, channel, status, COALESCE(is_debug, 0) AS is_debug,
               onboarding_state, created_at
        FROM accounts
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at ASC, id ASC
    """
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fetch_user_message_stats(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    date: str,
) -> Dict[str, Any]:
    """Count user-side messages for one account on one local calendar date."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS count,
               MIN(created_at) AS first_user_message_at,
               MAX(created_at) AS last_user_message_at
        FROM messages
        WHERE account_id = ?
          AND substr(created_at, 1, 10) = ?
          AND (direction = 'inbound' OR role = 'user')
        """,
        (account_id, date),
    ).fetchone()
    return {
        "count": int(row["count"] or 0) if row else 0,
        "first_user_message_at": row["first_user_message_at"] if row else None,
        "last_user_message_at": row["last_user_message_at"] if row else None,
    }


def analyze_account(conn: sqlite3.Connection, *, account: Dict[str, Any]) -> AccountRetention:
    """Analyze one account's D1 user-message retention."""
    account_id = str(account["id"])
    registered_at = str(account["created_at"] or "")
    registered_date = registered_at[:10]
    d1_date = _d1_date(registered_date)
    d0_stats = fetch_user_message_stats(conn, account_id=account_id, date=registered_date)
    d1_stats = fetch_user_message_stats(conn, account_id=account_id, date=d1_date)
    d1_count = int(d1_stats["count"])

    return AccountRetention(
        account_id=account_id,
        channel=account.get("channel"),
        status=str(account.get("status") or ""),
        is_debug=bool(account.get("is_debug")),
        onboarding_state=str(account.get("onboarding_state") or "pending"),
        registered_at=registered_at,
        registered_date=registered_date,
        d1_date=d1_date,
        d0_user_messages=int(d0_stats["count"]),
        d1_user_messages=d1_count,
        retained_d1=d1_count > 0,
        d1_first_user_message_at=d1_stats["first_user_message_at"],
        d1_last_user_message_at=d1_stats["last_user_message_at"],
    )


def _ratio(count: int, total: int) -> Optional[float]:
    """Return a rounded ratio or None when the denominator is empty."""
    if total <= 0:
        return None
    return round(count / total, 4)


def summarize_by_date(items: Iterable[AccountRetention]) -> Dict[str, Dict[str, Any]]:
    """Build D1 retention metrics by registration date."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in items:
        bucket = buckets.setdefault(
            item.registered_date,
            {
                "registered_count": 0,
                "retained_d1_count": 0,
                "d0_user_message_total": 0,
                "d1_user_message_total": 0,
            },
        )
        bucket["registered_count"] += 1
        bucket["retained_d1_count"] += 1 if item.retained_d1 else 0
        bucket["d0_user_message_total"] += item.d0_user_messages
        bucket["d1_user_message_total"] += item.d1_user_messages

    for bucket in buckets.values():
        total = int(bucket["registered_count"])
        retained = int(bucket["retained_d1_count"])
        bucket["d1_retention_rate"] = _ratio(retained, total)
        bucket["d0_user_message_avg"] = round(bucket["d0_user_message_total"] / total, 2) if total else None
        bucket["d1_user_message_avg"] = round(bucket["d1_user_message_total"] / total, 2) if total else None
    return dict(sorted(buckets.items()))


def summarize(items: List[AccountRetention]) -> Dict[str, Any]:
    """Build aggregate D1 retention metrics."""
    total = len(items)
    retained = sum(1 for item in items if item.retained_d1)
    d0_total = sum(item.d0_user_messages for item in items)
    d1_total = sum(item.d1_user_messages for item in items)
    by_date = summarize_by_date(items)
    return {
        "registered_count": total,
        "retained_d1_count": retained,
        "d1_retention_rate": _ratio(retained, total),
        "d0_user_message_total": d0_total,
        "d0_user_message_avg": round(d0_total / total, 2) if total else None,
        "d1_user_message_total": d1_total,
        "d1_user_message_avg": round(d1_total / total, 2) if total else None,
        "by_registered_date": by_date,
    }


def _percent(value: Optional[float]) -> str:
    """Format ratios for the human-readable table."""
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def print_text_report(
    *,
    summary: Dict[str, Any],
    items: Iterable[AccountRetention],
    details: bool,
) -> None:
    """Print a compact terminal report."""
    print("New-user D1 retention analysis")
    print(f"registered accounts: {summary['registered_count']}")
    print(f"D1 retained: {summary['retained_d1_count']} ({_percent(summary['d1_retention_rate'])})")
    print(
        "user messages: "
        f"D0 {summary['d0_user_message_total']} total, {summary['d0_user_message_avg']} avg/account; "
        f"D1 {summary['d1_user_message_total']} total, {summary['d1_user_message_avg']} avg/account"
    )
    print()
    print("registered_date | accounts | retained_d1 | retention | D0_user_msgs | D1_user_msgs")
    for date, bucket in summary["by_registered_date"].items():
        print(
            " | ".join(
                [
                    date,
                    str(bucket["registered_count"]),
                    str(bucket["retained_d1_count"]),
                    _percent(bucket["d1_retention_rate"]),
                    str(bucket["d0_user_message_total"]),
                    str(bucket["d1_user_message_total"]),
                ]
            )
        )
    print()
    print("definition: D1 retained means at least one message with direction='inbound' or role='user' on registration date + 1.")
    print("default: cohorts whose D1 is today or in the future are excluded; use --include-incomplete-d1 to include them.")

    if not details:
        return

    print()
    print("account_id | registered_date | d1_date | retained | D0_user_msgs | D1_user_msgs | first_D1_user_msg")
    for item in items:
        print(
            " | ".join(
                [
                    item.account_id,
                    item.registered_date,
                    item.d1_date,
                    "Y" if item.retained_d1 else "N",
                    str(item.d0_user_messages),
                    str(item.d1_user_messages),
                    item.d1_first_user_message_at or "",
                ]
            )
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line flags."""
    parser = argparse.ArgumentParser(
        description="Analyze new-user D1 retention from account registration date and user messages."
    )
    parser.add_argument("--db", help="SQLite path; defaults to settings.database_path")
    parser.add_argument("--account", help="Analyze one account_id")
    parser.add_argument("--start-date", help="Registration date lower bound, YYYY-MM-DD")
    parser.add_argument("--end-date", help="Registration date upper bound, YYYY-MM-DD")
    parser.add_argument("--as-of-date", help="Observation date, YYYY-MM-DD; defaults to local today")
    parser.add_argument(
        "--include-incomplete-d1",
        action="store_true",
        help="Include cohorts whose D1 is today or in the future",
    )
    parser.add_argument("--active-only", action="store_true", help="Only include accounts currently status='active'")
    parser.add_argument("--include-debug", action="store_true", help="Include accounts where is_debug=1")
    parser.add_argument("--details", action="store_true", help="Print per-account rows")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parse_args()
    start_date = _validate_date(args.start_date, "--start-date")
    end_date = _validate_date(args.end_date, "--end-date")
    as_of_date = _validate_date(args.as_of_date, "--as-of-date") or datetime.now().date().isoformat()
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date must be earlier than or equal to --end-date")

    db_path = _db_path(args.db)
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        accounts = fetch_accounts(
            conn,
            account_id=args.account,
            start_date=start_date,
            end_date=end_date,
            as_of_date=as_of_date,
            include_incomplete_d1=bool(args.include_incomplete_d1),
            active_only=bool(args.active_only),
            include_debug=bool(args.include_debug),
        )
        items = [analyze_account(conn, account=account) for account in accounts]
    finally:
        conn.close()

    output = {
        "filters": {
            "db": str(db_path),
            "account": args.account,
            "start_date": start_date,
            "end_date": end_date,
            "as_of_date": as_of_date,
            "include_incomplete_d1": bool(args.include_incomplete_d1),
            "eligible_registered_end_date": None
            if args.include_incomplete_d1
            else _eligible_end_date(as_of_date),
            "active_only": bool(args.active_only),
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
