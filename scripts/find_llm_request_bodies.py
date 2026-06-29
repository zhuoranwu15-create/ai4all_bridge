#!/usr/bin/env python3
"""Find exact dumped LLM request body files for a recent turn."""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _load_meta(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data["_meta_file"] = str(path)
    return data


def _body_path(meta: dict[str, Any]) -> Optional[Path]:
    raw = str(meta.get("body_file") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _body_contains(path: Path, text: str) -> bool:
    if not text:
        return True
    try:
        return text in path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return text.encode("utf-8") in path.read_bytes()
    except FileNotFoundError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Find exact dumped LLM request body files.")
    parser.add_argument("--root", default=str(ROOT / "tmp" / "llm_request_bodies"))
    parser.add_argument("--contains-text", required=True)
    parser.add_argument("--since-minutes", type=int, default=180)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_absolute():
        root = ROOT / root
    if not root.exists():
        print(f"dump root not found: {root}", file=sys.stderr)
        return 2

    cutoff = datetime.now() - timedelta(minutes=max(args.since_minutes, 0))
    matches: list[dict[str, Any]] = []
    for meta_path in root.rglob("*.meta.json"):
        meta = _load_meta(meta_path)
        if not meta:
            continue
        created_at = _parse_ts(meta.get("created_at"))
        if created_at is not None and created_at < cutoff:
            continue
        body = _body_path(meta)
        if body is None or not _body_contains(body, args.contains_text):
            continue
        matches.append(meta)

    matches.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    matches = matches[: max(args.limit, 1)]
    print(json.dumps({"matches": matches, "count": len(matches)}, ensure_ascii=False, indent=2))
    return 0 if matches else 2


if __name__ == "__main__":
    raise SystemExit(main())
