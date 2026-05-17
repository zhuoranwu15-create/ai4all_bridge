#!/usr/bin/env python3
"""Quick script to inspect the assembled system prompt for an account."""
import argparse
import json
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="86f866663cf9-im-bot")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    req = urllib.request.Request(
        f"{args.url}/debug/accounts/{args.account}/prompt-preview",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read().decode("utf-8"))

    b = d["blocks"]
    print(f"account    : {d['account_id']}")
    print(f"today      : {d['today']}")
    print(f"total_chars: {d['total_chars']}")
    print(f"--- blocks ---")
    print(f"  soul            : {b['soul_chars']} chars")
    print(f"  user_prefs      : {b['user_prefs_chars']} chars")
    print(f"  long_term_memory: {b['long_term_memory_chars']} chars")
    print(f"  daily_notes     : {b['daily_notes_chars']} chars")
    print(f"  style           : {b['style']}")
    print(f"  display_name    : {b['display_name']}")
    print(f"  override        : {b['system_prompt_override']}")
    print()
    print("=" * 60)
    print(d["prompt"])
    print("=" * 60)


if __name__ == "__main__":
    main()
