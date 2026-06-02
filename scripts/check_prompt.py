#!/usr/bin/env python3
"""Quick script to inspect the assembled system prompt for an account."""
import argparse
import json
import os
from urllib.parse import urlparse
import urllib.request


def resolve_base_url(raw_url: str) -> str:
    cleaned = (raw_url or "").strip().rstrip("/")
    if not cleaned:
        return "http://127.0.0.1:8000"
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("--url must be an absolute http(s) URL")
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="86f866663cf9-im-bot")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("ADMIN_TOKEN", "dev-admin-token"))
    args = parser.parse_args()

    base_url = resolve_base_url(args.url)
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    req = urllib.request.Request(
        f"{base_url}/debug/accounts/{args.account}/prompt-preview",
        headers=headers,
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
    prompt = d.get("prompt") or d.get("system_prompt")
    if prompt is None:
        prompt_chars = d.get("prompt_chars", d.get("system_prompt_chars", d.get("total_chars")))
        print(f"[prompt redacted; chars={prompt_chars}]")
    else:
        print(prompt)
    print("=" * 60)


if __name__ == "__main__":
    main()
