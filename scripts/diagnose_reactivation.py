"""
诊断 reactivation（topic_followup + content_invitation）候选和 dry-run 调度。

在生产服务器上直接运行（无需启动 HTTP 服务）：

    .venv/bin/python scripts/diagnose_reactivation.py
    .venv/bin/python scripts/diagnose_reactivation.py --account 86f866663cf9-im-bot
    .venv/bin/python scripts/diagnose_reactivation.py --run-planning --dispatch-dry-run
    .venv/bin/python scripts/diagnose_reactivation.py --json --output /tmp/reactivation.json

默认只读标准数据库。加 --run-planning 或 --dispatch-dry-run 时会复制 SQLite 到 /tmp，
所有候选写入、延后、清理都只发生在临时数据库；不会真实发送消息。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Dict, Iterator, List, Optional

# Allow running from repo root or scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.config import settings
    from app.db import (
        connect,
        get_account,
        get_proactive_account_state,
        list_accounts,
        list_outbound_messages,
        list_recent_messages_for_account_since,
        upsert_proactive_account_state,
    )
    from app.proactive.account_checks import (
        generate_content_invitation_candidate,
        generate_topic_followup_candidate,
    )
    from app.proactive.reactivation import (
        dispatch_reactivation_candidate,
        get_reactivation_candidate,
        plan_reactivation_candidate,
    )
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行：\n"
        "  .venv/bin/python scripts/diagnose_reactivation.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


Generator = Callable[..., Dict[str, Any]]


def _fmt(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _truncate_text(value: str, limit: int = 160) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@contextmanager
def temporary_database_copy() -> Iterator[str]:
    """Point repository DB helpers at a copied SQLite file for write-safe diagnosis."""
    original_path = str(getattr(settings, "database_path", "data/ai4all.sqlite3"))
    source = Path(original_path)
    if not source.is_absolute():
        source = Path.cwd() / source
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")

    with tempfile.TemporaryDirectory(prefix="ai4all_reactivation_") as tmp_dir:
        temp_path = Path(tmp_dir) / source.name
        src_conn = sqlite3.connect(str(source))
        dst_conn = sqlite3.connect(str(temp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        settings.database_path = str(temp_path)
        try:
            yield str(temp_path)
        finally:
            settings.database_path = original_path


def parse_now(value: Optional[str]) -> datetime:
    """Parse --now for reproducible diagnostics."""
    if not value:
        return datetime.now()
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("--now must look like '2026-06-05 12:15:00'")


def recent_chat_summary(
    *,
    account_id: str,
    now: datetime,
    hours: int = 72,
    sample_limit: int = 5,
) -> Dict[str, Any]:
    """Summarize account-isolated context used by topic_followup generation."""
    since_dt = now - timedelta(hours=max(int(hours), 1))
    since = _fmt(since_dt)
    try:
        context_limit = int(getattr(settings, "reactivation_topic_followup_context_messages", 100) or 100)
    except (TypeError, ValueError):
        context_limit = 100
    history = list_recent_messages_for_account_since(
        account_id=account_id,
        since=since,
        limit=max(context_limit, sample_limit, 1),
    )
    by_role: Dict[str, int] = {}
    for item in history:
        role = str(item.get("role") or "unknown")
        by_role[role] = by_role.get(role, 0) + 1
    samples = history[-sample_limit:] if sample_limit > 0 else []
    return {
        "since": since,
        "hours": hours,
        "total": len(history),
        "by_role": by_role,
        "latest_message_id": max((int(item["id"]) for item in history if item.get("id") is not None), default=None),
        "samples": [
            {
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "role": item.get("role"),
                "content": _truncate_text(str(item.get("content") or ""), 220),
            }
            for item in samples
        ],
    }


def today_chat_summary(
    *,
    account_id: str,
    now: datetime,
    sample_limit: int = 4,
) -> Dict[str, Any]:
    """Summarize today's chat records for quick content_invitation signal inspection."""
    today = now.date().isoformat()
    with connect() as conn:
        count_rows = conn.execute(
            """
            SELECT role, COUNT(*) AS count
            FROM messages
            WHERE account_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND substr(created_at, 1, 10) = ?
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
            GROUP BY role
            """,
            (account_id, today),
        ).fetchall()
        sample_rows = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE account_id = ?
              AND content IS NOT NULL
              AND content != ''
              AND substr(created_at, 1, 10) = ?
              AND NOT (
                role = 'assistant'
                AND error IS NOT NULL
                AND error != ''
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (account_id, today, max(sample_limit, 0)),
        ).fetchall()
    by_role = {str(row["role"] or "unknown"): int(row["count"] or 0) for row in count_rows}
    return {
        "date": today,
        "total": sum(by_role.values()),
        "by_role": by_role,
        "samples": [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "role": row["role"],
                "content": _truncate_text(str(row["content"] or ""), 220),
            }
            for row in reversed(sample_rows)
        ],
    }


def maybe_bootstrap_proactive_state(*, account_id: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Create minimal proactive state in the temporary DB for generation diagnosis."""
    if get_account(account_id=account_id) is None:
        return None
    if get_proactive_account_state(account_id=account_id) is not None:
        return None
    return upsert_proactive_account_state(
        account_id=account_id,
        enabled=True,
        next_scan_at=_fmt(now),
        metadata={"bootstrap_source": "diagnose_reactivation"},
    )


def compact_candidate(candidate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep diagnostic output readable while preserving the useful routing fields."""
    if not candidate:
        return None
    return {
        "id": candidate.get("id"),
        "type": candidate.get("type"),
        "topic": candidate.get("topic"),
        "text": candidate.get("text"),
        "reason": candidate.get("reason"),
        "confidence": candidate.get("confidence"),
        "generated_at": candidate.get("generated_at"),
        "scheduled_slot": candidate.get("scheduled_slot"),
        "scheduled_at": candidate.get("scheduled_at"),
        "content_invitation_id": candidate.get("content_invitation_id"),
        "source_message_cutoff_id": candidate.get("source_message_cutoff_id"),
        "dedupe": candidate.get("dedupe"),
        "policy": candidate.get("policy"),
    }


def compact_generation_result(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compact nested generator results for JSON/table output."""
    if not result:
        return None
    compact = {
        "action": result.get("action"),
        "reason": result.get("reason"),
        "evaluated_at": result.get("evaluated_at") or result.get("at"),
        "metadata": result.get("metadata") or {},
    }
    if isinstance(result.get("reactivation_candidate"), dict):
        compact["reactivation_candidate"] = compact_candidate(result.get("reactivation_candidate"))
    if isinstance(result.get("content_invitation"), dict):
        invitation = result["content_invitation"]
        compact["content_invitation"] = {
            "id": invitation.get("id"),
            "status": invitation.get("status"),
            "topic": invitation.get("topic"),
            "invitation_text": invitation.get("invitation_text"),
            "title_count": len(invitation.get("title_items") or []),
            "scheduled_at": invitation.get("scheduled_at"),
            "expires_at": invitation.get("expires_at"),
        }
    return compact


def run_planning_check(
    *,
    account_id: str,
    now: datetime,
    topic_followup_generator: Optional[Generator] = None,
    content_invitation_generator: Optional[Generator] = None,
) -> Dict[str, Any]:
    """Run the unified planning pass against the currently configured database."""
    result = plan_reactivation_candidate(
        account_id=account_id,
        now=now,
        topic_followup_generator=topic_followup_generator or generate_topic_followup_candidate,
        content_invitation_generator=content_invitation_generator or generate_content_invitation_candidate,
    )
    return {
        "action": result.get("action"),
        "reason": result.get("reason"),
        "reactivation_type": result.get("reactivation_type"),
        "evaluated_at": result.get("evaluated_at"),
        "reactivation_candidate": compact_candidate(result.get("reactivation_candidate")),
        "topic_followup_generation": compact_generation_result(result.get("topic_followup_generation")),
        "content_invitation_generation": compact_generation_result(result.get("content_invitation_generation")),
    }


def _diagnose_dedupe_checker(**_: Any) -> Dict[str, Any]:
    return {
        "checked": True,
        "duplicate": False,
        "reason": "diagnose_assumed_not_duplicate",
        "retry_count": 0,
    }


def run_dispatch_dry_run(
    *,
    account_id: str,
    now: datetime,
    use_llm_dedupe: bool = False,
) -> Dict[str, Any]:
    """Run dispatch revalidation in dry-run mode; no outbound row or gateway send is created."""
    before_count = len(list_outbound_messages(account_id=account_id, limit=200))
    result = dispatch_reactivation_candidate(
        account_id=account_id,
        now=now,
        dry_run=True,
        dedupe_checker=None if use_llm_dedupe else _diagnose_dedupe_checker,
    )
    after_count = len(list_outbound_messages(account_id=account_id, limit=200))
    compact = {
        "action": result.get("action"),
        "reason": result.get("reason"),
        "evaluated_at": result.get("evaluated_at"),
        "scheduled_at": result.get("scheduled_at"),
        "text": result.get("text"),
        "recent_inbound_count": result.get("recent_inbound_count"),
        "avoidance_count": result.get("avoidance_count"),
        "reactivation_candidate": compact_candidate(result.get("reactivation_candidate")),
        "outbound_metadata": result.get("outbound_metadata"),
        "outbound_rows_before": before_count,
        "outbound_rows_after": after_count,
        "created_outbound_rows": max(after_count - before_count, 0),
    }
    return {key: value for key, value in compact.items() if value is not None}


def diagnose_account(
    *,
    account_id: str,
    now: datetime,
    sample_limit: int = 5,
    bootstrap_state: bool = False,
    run_planning: bool = False,
    dispatch_dry_run: bool = False,
    include_no_recent_chat: bool = False,
    use_llm_dedupe: bool = False,
) -> Dict[str, Any]:
    recent = recent_chat_summary(
        account_id=account_id,
        now=now,
        sample_limit=sample_limit,
    )
    today = today_chat_summary(
        account_id=account_id,
        now=now,
        sample_limit=min(sample_limit, 4),
    )
    bootstrapped = maybe_bootstrap_proactive_state(account_id=account_id, now=now) if bootstrap_state else None
    current_candidate_before = compact_candidate(get_reactivation_candidate(account_id=account_id))

    planning = None
    if run_planning:
        if not include_no_recent_chat and int(recent.get("total") or 0) <= 0:
            planning = {
                "action": "no_op",
                "reason": "no_recent_72h_history",
                "evaluated_at": _fmt(now),
            }
        else:
            planning = run_planning_check(account_id=account_id, now=now)

    current_candidate_after_planning = compact_candidate(get_reactivation_candidate(account_id=account_id))
    dispatch = run_dispatch_dry_run(
        account_id=account_id,
        now=now,
        use_llm_dedupe=use_llm_dedupe,
    ) if dispatch_dry_run else None

    candidate = current_candidate_after_planning or current_candidate_before
    if dispatch and dispatch.get("action") == "would_send":
        verdict = f"would_send:{candidate.get('type') if candidate else 'unknown'}"
    elif dispatch and dispatch.get("action"):
        verdict = f"dispatch_{dispatch.get('action')}:{dispatch.get('reason') or 'ok'}"
    elif planning and planning.get("action") == "reactivation_candidate_planned":
        verdict = f"planned:{planning.get('reactivation_type')}"
    elif planning:
        verdict = f"not_planned:{planning.get('reason') or planning.get('action')}"
    elif candidate:
        verdict = f"has_candidate:{candidate.get('type')}"
    else:
        verdict = "no_candidate"

    return {
        "account_id": account_id,
        "verdict": verdict,
        "recent_72h_chat": recent,
        "today_chat": today,
        "current_candidate_before": current_candidate_before,
        "planning_run": planning,
        "current_candidate_after_planning": current_candidate_after_planning,
        "dispatch_dry_run": dispatch,
        "bootstrapped_proactive_state": bool(bootstrapped),
    }


def write_result_file(results: List[Dict[str, Any]], *, output: Optional[str] = None) -> str:
    """Persist diagnostic results outside the production DB for later review."""
    if output:
        path = Path(output)
    else:
        path = Path(tempfile.gettempdir()) / (
            "ai4all_reactivation_diagnosis_"
            + datetime.now().strftime("%Y%m%d%H%M%S")
            + ".json"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": _fmt(datetime.now()),
        "database_path": str(getattr(settings, "database_path", "")),
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _print_table(results: List[Dict[str, Any]], *, result_file: Optional[str] = None) -> None:
    print(f"\n{'='*78}")
    print(f"  Reactivation 诊断  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("  reactivation = topic_followup + content_invitation  |  dispatch=dry-run only")
    print(f"{'='*78}")

    for item in results:
        candidate = item.get("current_candidate_after_planning") or item.get("current_candidate_before")
        recent = item.get("recent_72h_chat") or {}
        today = item.get("today_chat") or {}
        planning = item.get("planning_run")
        dispatch = item.get("dispatch_dry_run")
        symbol = "✓" if str(item.get("verdict", "")).startswith(("would_send", "planned", "has_candidate")) else "·"

        print(f"\n{symbol} {item['account_id']}")
        print(f"  verdict      : {item.get('verdict')}")
        if item.get("bootstrapped_proactive_state"):
            print("  bootstrap    : created proactive_account_state(enabled=True) in temp DB")
        print(
            "  recent 72h   : "
            f"total={recent.get('total', 0)}  by_role={recent.get('by_role', {})}  "
            f"since={recent.get('since')}"
        )
        print(
            "  today chat   : "
            f"date={today.get('date')}  total={today.get('total', 0)}  "
            f"by_role={today.get('by_role', {})}"
        )
        for sample in recent.get("samples") or []:
            print(
                "    recent     : "
                f"#{sample.get('id')} [{sample.get('created_at')}] "
                f"{sample.get('role')}: {sample.get('content')}"
            )

        if candidate:
            print(
                "  candidate    : "
                f"type={candidate.get('type')}  id={candidate.get('id')}  "
                f"topic={candidate.get('topic')}  scheduled={candidate.get('scheduled_at')}"
            )
            print(f"    text       : {candidate.get('text')}")
        else:
            print("  candidate    : none")

        if planning:
            print(
                "  planning     : "
                f"action={planning.get('action')}  "
                f"type={planning.get('reactivation_type')}  reason={planning.get('reason')}"
            )
            topic_gen = planning.get("topic_followup_generation") or {}
            content_gen = planning.get("content_invitation_generation") or {}
            if topic_gen:
                print(
                    "    topic      : "
                    f"action={topic_gen.get('action')}  reason={topic_gen.get('reason')}"
                )
            if content_gen:
                print(
                    "    content    : "
                    f"action={content_gen.get('action')}  reason={content_gen.get('reason')}"
                )

        if dispatch:
            print(
                "  dispatch     : "
                f"action={dispatch.get('action')}  reason={dispatch.get('reason')}  "
                f"outbound_delta={dispatch.get('created_outbound_rows')}"
            )
            if dispatch.get("text"):
                print(f"    would send : {dispatch.get('text')}")

    verdicts: Dict[str, int] = {}
    for item in results:
        verdict = str(item.get("verdict") or "unknown")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    print(f"\n{'-'*78}")
    print(f"  汇总（共 {len(results)} 个账号）：")
    for verdict, count in sorted(verdicts.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"    {count:3d}  {verdict}")
    if result_file:
        print(f"\n  result file: {result_file}")
    print(f"{'='*78}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断 reactivation 候选和 dry-run 调度")
    parser.add_argument("--account", help="只诊断指定账号")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--sample-limit", type=int, default=5, help="每个账号展示的最近聊天样本条数")
    parser.add_argument("--now", help="指定诊断时间，例如 '2026-06-05 12:15:00'")
    parser.add_argument(
        "--run-planning",
        action="store_true",
        help="调用真实 topic_followup/content_invitation 生成器；可能发起 LLM/tool 调用，但只写临时数据库",
    )
    parser.add_argument(
        "--dispatch-dry-run",
        action="store_true",
        help="在临时数据库中执行到点发送 revalidation；不会创建 outbound，不会调用 OpenClaw",
    )
    parser.add_argument(
        "--llm-dedupe",
        action="store_true",
        help="dispatch dry-run 时使用真实 LLM 去重；默认用 no-duplicate 诊断桩，避免额外 LLM 调用",
    )
    parser.add_argument(
        "--bootstrap-state",
        action="store_true",
        help="在临时数据库中为缺失 proactive_account_state 的账号创建最小启用状态；--run-planning 默认启用",
    )
    parser.add_argument(
        "--strict-state",
        action="store_true",
        help="--run-planning 时不临时补 proactive_account_state，严格复现生产 gate",
    )
    parser.add_argument(
        "--include-no-recent-chat",
        action="store_true",
        help="即使账号最近 72 小时没有聊天记录，也执行 planning 探测",
    )
    parser.add_argument("--output", help="JSON 结果文件路径；默认写入 /tmp")
    args = parser.parse_args()

    now = parse_now(args.now)
    accounts = [{"id": args.account}] if args.account else [
        account for account in list_accounts() if account.get("status") == "active"
    ]
    needs_temp_db = args.run_planning or args.dispatch_dry_run
    result_file = None

    def collect_results() -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for account in accounts:
            account_id = account["id"]
            try:
                results.append(
                    diagnose_account(
                        account_id=account_id,
                        now=now,
                        sample_limit=args.sample_limit,
                        bootstrap_state=(
                            needs_temp_db
                            and (
                                args.bootstrap_state
                                or (args.run_planning and not args.strict_state)
                            )
                        ),
                        run_planning=args.run_planning,
                        dispatch_dry_run=args.dispatch_dry_run,
                        include_no_recent_chat=args.include_no_recent_chat,
                        use_llm_dedupe=args.llm_dedupe,
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "account_id": account_id,
                        "verdict": f"error: {exc}",
                        "recent_72h_chat": {},
                        "today_chat": {},
                        "current_candidate_before": None,
                        "planning_run": None,
                        "current_candidate_after_planning": None,
                        "dispatch_dry_run": None,
                        "bootstrapped_proactive_state": False,
                    }
                )
        return results

    if needs_temp_db:
        with temporary_database_copy() as temp_db_path:
            results = collect_results()
            result_file = write_result_file(results, output=args.output)
            for item in results:
                item["temporary_database_path"] = temp_db_path
    else:
        results = collect_results()
        if args.output:
            result_file = write_result_file(results, output=args.output)

    if args.as_json:
        print(json.dumps({"result_file": result_file, "results": results}, ensure_ascii=False, indent=2))
    else:
        _print_table(results, result_file=result_file)


if __name__ == "__main__":
    main()
