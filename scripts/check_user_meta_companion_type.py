#!/usr/bin/env python3
"""Inspect and optionally run the user-meta companion type classifier."""
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import fetch_recent_inbound_messages, init_db  # noqa: E402
from app.prompts.user_meta_companion_type import build_companion_classify_prompt  # noqa: E402
from app.user_meta_scheduler import classify_companion_type  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="aid_806382741")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-prompt", action="store_true")
    args = parser.parse_args()

    init_db()
    messages = fetch_recent_inbound_messages(
        account_id=args.account,
        limit=args.limit,
    )
    prompt = build_companion_classify_prompt(messages=messages)
    print(f"account: {args.account}")
    print(f"messages: {len(messages)}")
    print(f"prompt_chars: {len(prompt)}")
    if args.show_prompt or args.dry_run:
        print("=" * 60)
        print(prompt)
        print("=" * 60)
    if args.dry_run:
        return 0

    result = classify_companion_type(messages=messages)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
