#!/usr/bin/env python3
"""Export the latest AI4ALL/OpenClaw prompt trace pair for one account."""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402

DEFAULT_ACCOUNT_ID = "aid_806382741"
DEFAULT_MESSAGE_TEXT = "查查今日金价"


def _decode_json(raw: Any, fallback: Any) -> Any:
    """Decode a JSON text field from debug_traces, preserving a sane fallback."""
    if raw is None or raw == "":
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _load_traces(
    *,
    account_id: str,
    message_id: Optional[str],
    since_minutes: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Load recent prompt traces for one isolated account."""
    clauses = ["account_id = ?", "source IN ('ai4all', 'openclaw')"]
    params: list[Any] = [account_id]
    if message_id:
        clauses.append("message_id = ?")
        params.append(message_id)
    if since_minutes > 0:
        since = datetime.now() - timedelta(minutes=since_minutes)
        clauses.append("created_at >= ?")
        params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
    params.append(limit)

    sql = f"""
        SELECT
            id, trace_id, account_id, session_id, message_id, source,
            llm_model, system_prompt, messages_json, reply,
            metadata_json, latency_ms, error, created_at
        FROM debug_traces
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT ?
    """
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(sql, params).fetchall()

    traces: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["messages"] = _decode_json(item.pop("messages_json"), [])
        item["metadata"] = _decode_json(item.pop("metadata_json"), {})
        traces.append(item)
    return traces


def _content_to_text(content: Any) -> str:
    """Flatten common OpenAI/OpenClaw message content shapes into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(_content_to_text(item.get("text") or item.get("content")))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _trace_text(trace: dict[str, Any]) -> str:
    """Return searchable trace text without changing the exported payload."""
    parts = [
        str(trace.get("system_prompt") or ""),
        str(trace.get("reply") or ""),
    ]
    for message in trace.get("messages") or []:
        if isinstance(message, dict):
            parts.append(_content_to_text(message.get("content")))
    return "\n".join(parts)


def _contains_text(trace: dict[str, Any], text: str) -> bool:
    """Check whether a trace appears to belong to the requested user message."""
    needle = (text or "").strip()
    if not needle:
        return True
    return needle in _trace_text(trace)


def _parse_created_at(value: Any) -> Optional[datetime]:
    """Parse the DB timestamp format used by debug_traces."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _find_pair(
    traces: list[dict[str, Any]],
    *,
    contains_text: str,
    allow_time_fallback: bool,
    pair_window_seconds: int,
) -> Optional[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Find the newest ai4all/openclaw trace pair, preferring shared message_id."""
    by_message: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trace in traces:
        message_id = str(trace.get("message_id") or "").strip()
        source = str(trace.get("source") or "")
        if not message_id or source not in {"ai4all", "openclaw"}:
            continue
        if contains_text and not _contains_text(trace, contains_text):
            continue
        existing = by_message[message_id].get(source)
        if existing is None or int(trace["id"]) > int(existing["id"]):
            by_message[message_id][source] = trace

    groups = sorted(
        by_message.items(),
        key=lambda kv: max(int(t["id"]) for t in kv[1].values()),
        reverse=True,
    )
    for message_id, group in groups:
        if "ai4all" in group and "openclaw" in group:
            return f"message_id:{message_id}", group["ai4all"], group["openclaw"]

    if not allow_time_fallback:
        return None

    ai4all = [t for t in traces if t.get("source") == "ai4all" and _contains_text(t, contains_text)]
    openclaw = [t for t in traces if t.get("source") == "openclaw" and _contains_text(t, contains_text)]
    best: Optional[tuple[int, dict[str, Any], dict[str, Any]]] = None
    for left in ai4all:
        left_ts = _parse_created_at(left.get("created_at"))
        if left_ts is None:
            continue
        for right in openclaw:
            right_ts = _parse_created_at(right.get("created_at"))
            if right_ts is None:
                continue
            delta = abs(int((left_ts - right_ts).total_seconds()))
            if delta > pair_window_seconds:
                continue
            if best is None or delta < best[0]:
                best = (delta, left, right)
    if best is None:
        return None
    return f"time_window:{best[0]}s", best[1], best[2]


def _safe_filename(value: Any) -> str:
    """Make a compact filesystem-safe label."""
    text = str(value or "none").strip()
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            cleaned.append(ch)
        else:
            cleaned.append("_")
    return "".join(cleaned).strip("_")[:96] or "none"


def _fence(text: Any, lang: str = "text") -> str:
    """Build a markdown code fence that cannot be closed by the content."""
    body = "" if text is None else str(text)
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return f"{ticks}{lang}\n{body}\n{ticks}\n"


def _markdown_for_trace(trace: dict[str, Any]) -> str:
    """Render a trace as a human-readable prompt comparison document."""
    lines = [
        f"# {str(trace.get('source') or '').upper()} Prompt Trace",
        "",
        f"- trace_id: {trace.get('trace_id')}",
        f"- account_id: {trace.get('account_id')}",
        f"- message_id: {trace.get('message_id')}",
        f"- session_id: {trace.get('session_id')}",
        f"- llm_model: {trace.get('llm_model')}",
        f"- created_at: {trace.get('created_at')}",
        f"- latency_ms: {trace.get('latency_ms')}",
        f"- error: {trace.get('error')}",
        "",
        "## System Prompt",
        _fence(trace.get("system_prompt") or ""),
        "## Messages",
        "",
    ]
    for index, message in enumerate(trace.get("messages") or []):
        role = message.get("role") if isinstance(message, dict) else "unknown"
        lines.append(f"### {index}. {role}")
        if isinstance(message, dict):
            if "source" in message:
                lines.append(f"- source: {message.get('source')}")
            lines.append(_fence(_content_to_text(message.get("content"))))
            extra_keys = sorted(set(message.keys()) - {"role", "content", "source"})
            if extra_keys:
                extra = {key: message.get(key) for key in extra_keys}
                lines.append("Extra fields:")
                lines.append(_fence(json.dumps(extra, ensure_ascii=False, indent=2), "json"))
        else:
            lines.append(_fence(message))
    lines.extend(
        [
            "## Reply",
            _fence(trace.get("reply") or ""),
            "## Metadata",
            _fence(json.dumps(trace.get("metadata") or {}, ensure_ascii=False, indent=2), "json"),
        ]
    )
    return "\n".join(lines)


def _write_trace(out_dir: Path, base: str, trace: dict[str, Any]) -> dict[str, Any]:
    """Write one trace as JSON plus Markdown and return file metadata."""
    source = _safe_filename(trace.get("source"))
    json_path = out_dir / f"{base}-{source}.json"
    md_path = out_dir / f"{base}-{source}.md"
    json_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown_for_trace(trace), encoding="utf-8")
    return {
        "source": trace.get("source"),
        "trace_id": trace.get("trace_id"),
        "json": str(json_path),
        "markdown": str(md_path),
        "messages_count": len(trace.get("messages") or []),
        "system_prompt_chars": len(str(trace.get("system_prompt") or "")),
    }


def _print_candidates(traces: list[dict[str, Any]]) -> None:
    """Print concise diagnostics when no pair is available yet."""
    print("No matched ai4all/openclaw trace pair found.", file=sys.stderr)
    if not traces:
        print("No recent candidate traces for this account.", file=sys.stderr)
        return
    print("Recent candidates:", file=sys.stderr)
    for trace in traces[:12]:
        print(
            "- "
            f"id={trace.get('id')} source={trace.get('source')} "
            f"message_id={trace.get('message_id')} trace_id={trace.get('trace_id')} "
            f"created_at={trace.get('created_at')}",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a recent AI4ALL/OpenClaw prompt trace pair from debug_traces."
    )
    parser.add_argument("--account", default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--out-dir", default=str(ROOT / "tmp" / "prompt_traces"))
    parser.add_argument("--message-id", default=None)
    parser.add_argument(
        "--contains-text",
        default=DEFAULT_MESSAGE_TEXT,
        help="Only export a pair whose trace text contains this string. Use '' to disable.",
    )
    parser.add_argument(
        "--since-minutes",
        type=int,
        default=1440,
        help="Recent trace window. debug_traces.created_at may be UTC while messages are Beijing-local.",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--wait-seconds", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--pair-window-seconds", type=int, default=600)
    parser.add_argument(
        "--no-time-fallback",
        action="store_true",
        help="Require ai4all/openclaw traces to share the same message_id.",
    )
    args = parser.parse_args()

    deadline = time.monotonic() + max(args.wait_seconds, 0)
    traces: list[dict[str, Any]] = []
    pair: Optional[tuple[str, dict[str, Any], dict[str, Any]]] = None
    while True:
        traces = _load_traces(
            account_id=args.account,
            message_id=args.message_id,
            since_minutes=max(args.since_minutes, 0),
            limit=max(args.limit, 1),
        )
        pair = _find_pair(
            traces,
            contains_text=args.contains_text,
            allow_time_fallback=not args.no_time_fallback and not args.message_id,
            pair_window_seconds=max(args.pair_window_seconds, 1),
        )
        if pair is not None or time.monotonic() >= deadline:
            break
        time.sleep(max(args.poll_interval, 0.25))

    if pair is None:
        _print_candidates(traces)
        return 2

    match_kind, ai4all_trace, openclaw_trace = pair
    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    exported_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    message_label = ai4all_trace.get("message_id") or openclaw_trace.get("message_id")
    base = "-".join(
        [
            exported_at,
            _safe_filename(args.account),
            _safe_filename(message_label),
        ]
    )

    summary = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "account_id": args.account,
        "match_kind": match_kind,
        "contains_text": args.contains_text,
        "db_path": str(db_path),
        "traces": {
            "ai4all": _write_trace(out_dir, base, ai4all_trace),
            "openclaw": _write_trace(out_dir, base, openclaw_trace),
        },
    }
    summary_path = out_dir / f"{base}-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Exported prompt trace pair ({match_kind})")
    print(f"summary: {summary_path}")
    for source, info in summary["traces"].items():
        print(
            f"{source}: {info['json']} | {info['markdown']} "
            f"(messages={info['messages_count']}, system_prompt_chars={info['system_prompt_chars']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
