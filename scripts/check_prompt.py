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
    build_body = json.dumps({"include_tool_instructions": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/debug/prompt-lab/accounts/{args.account}/build",
        data=build_body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Accounts without any session still support the compatibility wrapper,
        # which now uses the same backend prompt builder.
        req = urllib.request.Request(
            f"{base_url}/debug/accounts/{args.account}/prompt-preview",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))

    metadata = d.get("metadata") or {}
    b = d.get("blocks") or metadata
    prompt = d.get("prompt") or d.get("system_prompt")
    total_chars = d.get(
        "total_chars",
        d.get("system_prompt_chars", len(prompt or "")),
    )
    print(f"account    : {d['account_id']}")
    print(f"today      : {d['today']}")
    print(f"total_chars: {total_chars}")
    print(f"--- blocks ---")
    print(f"  soul            : {b.get('soul_chars', 0)} chars")
    print(f"  user_prefs      : {b.get('user_prefs_chars', 0)} chars")
    print(f"  long_term_memory: {b.get('long_term_memory_chars', 0)} chars")
    print(f"  daily_notes     : {b.get('daily_notes_chars', 0)} chars")
    print(f"  style           : {b.get('style')}")
    print(f"  display_name    : {b.get('display_name')}")
    print(f"  override        : {b.get('system_prompt_override')}")
    tooling = d.get("tooling") or metadata.get("tooling") or {}
    if tooling:
        print(f"  tools           : {', '.join(tooling.get('available_tool_names') or [])}")
    print()
    print("=" * 60)
    if prompt is None:
        prompt_chars = d.get("prompt_chars", d.get("system_prompt_chars", d.get("total_chars")))
        print(f"[prompt redacted; chars={prompt_chars}]")
    else:
        print(prompt)
    print("=" * 60)


if __name__ == "__main__":
    main()
