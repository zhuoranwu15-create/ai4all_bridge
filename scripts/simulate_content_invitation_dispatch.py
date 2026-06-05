"""
模拟 content invitation 到点发送时用户会看到的邀请消息。

示例：
    .venv/bin/python scripts/simulate_content_invitation_dispatch.py \
      --result-file /tmp/ai4all_content_invitation_diagnosis_20260604233552.json

    .venv/bin/python scripts/simulate_content_invitation_dispatch.py \
      --from-db --account aid_575702401 --send-at "2026-06-06 12:00:00"

说明：真实 dispatch 不会重新生成文案，也不会自动加“中午好”。
发送文本就是 content_invitations.invitation_text。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Allow running from repo root or scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.db import list_accounts, list_content_invitations_for_account
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行。",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


def _default_send_at() -> str:
    tomorrow_noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return tomorrow_noon.strftime("%Y-%m-%d %H:%M:%S")


def _compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _candidate_from_generation_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    invitation = (result.get("generation_run") or {}).get("content_invitation")
    if not invitation:
        return None
    return {
        "account_id": result.get("account_id"),
        "invitation_id": invitation.get("id"),
        "topic": invitation.get("topic"),
        "invitation_text": invitation.get("invitation_text"),
        "title_count": invitation.get("title_count"),
        "titles": invitation.get("titles") or [],
        "source": "result_file",
    }


def load_candidates_from_result_files(paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Read generated candidates from diagnose_content_invitations JSON result files."""
    candidates: List[Dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            candidate = _candidate_from_generation_result(result)
            if candidate:
                candidate["result_file"] = str(path)
                candidates.append(candidate)
    return candidates


def load_candidates_from_db(*, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read current DB candidate invitations without dispatching them."""
    accounts = [{"id": account_id}] if account_id else [a for a in list_accounts() if a.get("status") == "active"]
    candidates: List[Dict[str, Any]] = []
    for account in accounts:
        aid = str(account["id"])
        for invitation in list_content_invitations_for_account(account_id=aid, status="candidate", limit=20):
            titles = invitation.get("title_items") or []
            candidates.append(
                {
                    "account_id": aid,
                    "invitation_id": invitation.get("id"),
                    "topic": invitation.get("topic"),
                    "invitation_text": invitation.get("invitation_text"),
                    "title_count": len(titles),
                    "titles": titles,
                    "source": "db",
                }
            )
    return candidates


def simulate_dispatch_rows(
    candidates: Iterable[Dict[str, Any]],
    *,
    send_at: str,
    account_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build dispatch previews. The text mirrors the real dispatcher."""
    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        aid = str(candidate.get("account_id") or "")
        if account_id and aid != account_id:
            continue
        titles = candidate.get("titles") or []
        rows.append(
            {
                "account_id": aid,
                "send_at": send_at,
                "source": candidate.get("source"),
                "content_invitation_id": candidate.get("invitation_id"),
                "topic": candidate.get("topic"),
                "actual_dispatch_text": _compact_text(candidate.get("invitation_text")),
                "note": "真实 dispatch 直接发送 actual_dispatch_text，不会二次按人设改写。",
                "title_count": candidate.get("title_count") if candidate.get("title_count") is not None else len(titles),
                "titles": [
                    {
                        "title": item.get("title"),
                        "source_name": item.get("source_name"),
                        "url": item.get("url"),
                    }
                    for item in titles
                ],
            }
        )
    return rows


def print_rows(rows: List[Dict[str, Any]]) -> None:
    print(f"\n{'=' * 72}")
    print(f"  内容邀请发送模拟  |  candidates={len(rows)}")
    print(f"{'=' * 72}")
    for row in rows:
        print(f"\n{row['account_id']}")
        print(f"  send_at : {row['send_at']}")
        print(f"  topic   : {row.get('topic')}")
        print(f"  text    : {row.get('actual_dispatch_text')}")
        print(f"  titles  : {row.get('title_count')}")
        for title in (row.get("titles") or [])[:5]:
            print(f"    - {title.get('title')}")
    print(f"{'=' * 72}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟内容邀请到点发送文本")
    parser.add_argument("--result-file", action="append", default=[], help="diagnose_content_invitations 输出 JSON，可传多次")
    parser.add_argument("--from-db", action="store_true", help="从当前数据库读取 candidate invitations")
    parser.add_argument("--account", help="只模拟指定账号")
    parser.add_argument("--send-at", default=_default_send_at(), help="模拟发送时间，默认明天中午")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    args = parser.parse_args()

    candidates: List[Dict[str, Any]] = []
    if args.result_file:
        candidates.extend(load_candidates_from_result_files(args.result_file))
    if args.from_db:
        candidates.extend(load_candidates_from_db(account_id=args.account))
    if not args.result_file and not args.from_db:
        parser.error("请传 --result-file 或 --from-db")

    rows = simulate_dispatch_rows(candidates, send_at=args.send_at, account_id=args.account)
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_rows(rows)


if __name__ == "__main__":
    main()
