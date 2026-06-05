"""
诊断内容邀请策略触发情况。

在生产服务器上直接运行（无需启动 HTTP 服务）：

    .venv/bin/python scripts/diagnose_content_invitations.py
    .venv/bin/python scripts/diagnose_content_invitations.py --account 86f866663cf9-im-bot
    .venv/bin/python scripts/diagnose_content_invitations.py --json
    .venv/bin/python scripts/diagnose_content_invitations.py --run-generation

输出：每个账号的生成前置检查结果、今日聊天摘要，以及调度层策略评估。
加 --run-generation 时会调用 LLM，但只写入 /tmp 下的临时数据库和 JSON 结果文件，
不会写入标准数据库。
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

# Allow running from repo root or scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.config import settings
    from app.db import (
        connect,
        get_account,
        get_active_content_invitation,
        get_pending_companion_followup_count_in_window,
        get_pending_reminder_count_in_window,
        get_proactive_account_state,
        list_accounts,
        list_channel_bindings_for_account,
        list_content_invitations_for_account,
        list_recent_messages,
        list_sessions_for_account,
        upsert_proactive_account_state,
    )
    from app.proactive.policy import (
        OutboundCategory,
        evaluate_outbound_policy,
        is_quiet_hours,
    )
    from app.proactive.account_checks import generate_content_invitation_candidate
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行：\n"
        "  .venv/bin/python scripts/diagnose_content_invitations.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


def _fmt(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _compact_invitation(invitation: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not invitation:
        return None
    title_items = invitation.get("title_items") or []
    return {
        "id": invitation.get("id"),
        "status": invitation.get("status"),
        "topic": invitation.get("topic"),
        "invitation_text": invitation.get("invitation_text"),
        "title_count": len(title_items),
        "titles": [
            {
                "title": item.get("title"),
                "source_name": item.get("source_name"),
                "url": item.get("url"),
            }
            for item in title_items
        ],
        "scheduled_at": invitation.get("scheduled_at"),
        "expires_at": invitation.get("expires_at"),
    }


@contextmanager
def temporary_database_copy() -> Iterator[str]:
    """Point repository DB helpers at a copied SQLite file for write-safe dry-runs."""
    original_path = str(getattr(settings, "database_path", "data/ai4all.sqlite3"))
    source = Path(original_path)
    if not source.is_absolute():
        source = Path.cwd() / source
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")

    with tempfile.TemporaryDirectory(prefix="ai4all_content_invitation_") as tmp_dir:
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


def _select_route(account_id: str) -> Optional[Dict[str, Any]]:
    for binding in list_channel_bindings_for_account(account_id=account_id):
        chat_id = str(binding.get("chat_id") or "").strip()
        channel_account_id = str(binding.get("channel_account_id") or "").strip()
        if chat_id and channel_account_id:
            return binding
    return None


def _truncate_text(value: str, limit: int = 120) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def today_chat_summary(
    *,
    account_id: str,
    today: str,
    sample_limit: int = 4,
) -> Dict[str, Any]:
    """Summarize today's account-isolated chat records used as diagnostic evidence."""
    if sample_limit <= 0:
        sample_limit = 0

    with connect() as conn:
        count_rows = conn.execute(
            """
            SELECT m.role, COUNT(*) AS count
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.account_id = ?
              AND m.content IS NOT NULL
              AND m.content != ''
              AND (
                    substr(m.created_at, 1, 10) = ?
                    OR s.business_day = ?
              )
              AND NOT (
                m.role = 'assistant'
                AND m.error IS NOT NULL
                AND m.error != ''
              )
            GROUP BY m.role
            """,
            (account_id, today, today),
        ).fetchall()
        sample_rows = conn.execute(
            """
            SELECT m.role, m.content, m.created_at, s.business_day
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.account_id = ?
              AND m.content IS NOT NULL
              AND m.content != ''
              AND (
                    substr(m.created_at, 1, 10) = ?
                    OR s.business_day = ?
              )
              AND NOT (
                m.role = 'assistant'
                AND m.error IS NOT NULL
                AND m.error != ''
              )
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (account_id, today, today, sample_limit),
        ).fetchall()

    by_role = {str(row["role"] or "unknown"): int(row["count"] or 0) for row in count_rows}
    return {
        "date": today,
        "total": sum(by_role.values()),
        "by_role": by_role,
        "samples": [
            {
                "role": row["role"],
                "created_at": row["created_at"],
                "business_day": row["business_day"],
                "content": _truncate_text(str(row["content"] or ""), 180),
            }
            for row in reversed(sample_rows)
        ],
    }


def recent_history_summary(
    *,
    account_id: str,
    sample_limit: int = 4,
) -> Dict[str, Any]:
    """Return the latest session context sample that generation would inspect."""
    sessions = list_sessions_for_account(account_id=account_id, limit=1)
    if not sessions:
        return {"session_id": None, "total_sampled": 0, "samples": []}

    try:
        history_limit = int(getattr(settings, "proactive_account_check_context_messages", 12) or 12)
    except (TypeError, ValueError):
        history_limit = 12
    history = list_recent_messages(
        session_id=int(sessions[0]["id"]),
        limit=max(1, history_limit),
    )
    samples = history[-sample_limit:] if sample_limit > 0 else []
    return {
        "session_id": sessions[0]["id"],
        "total_sampled": len(history),
        "samples": [
            {
                "role": item["role"],
                "content": _truncate_text(str(item.get("content") or ""), 180),
            }
            for item in samples
        ],
    }


def check_generation_gates(*, account_id: str, now: datetime) -> Dict[str, Any]:
    """
    走一遍 generate_content_invitation_candidate 的所有前置检查，不发 LLM 请求。
    返回第一个拦截点（或 "ok" 表示可以进入 LLM）。
    """
    now_text = _fmt(now)

    account = get_account(account_id=account_id)
    if account is None:
        return {"gate": "account_not_found", "ok": False}
    if account.get("status") != "active":
        return {"gate": "account_not_active", "ok": False}

    state = get_proactive_account_state(account_id=account_id)
    if state is None:
        return {"gate": "proactive_state_missing", "ok": False}
    if not state.get("enabled"):
        return {"gate": "proactive_disabled", "ok": False}

    if not getattr(settings, "llm_api_key", ""):
        return {"gate": "llm_disabled", "ok": False}

    route = _select_route(account_id)
    if route is None:
        return {"gate": "missing_channel_route", "ok": False}

    active = get_active_content_invitation(account_id=account_id, now=now_text)
    if active is not None:
        return {
            "gate": "active_invitation_exists",
            "ok": False,
            "invitation_id": active["id"],
            "status": active["status"],
        }

    candidates = list_content_invitations_for_account(account_id=account_id, status="candidate", limit=1)
    if candidates:
        return {
            "gate": "candidate_exists",
            "ok": False,
            "invitation_id": candidates[0]["id"],
        }

    try:
        avoidance_hours = int(getattr(settings, "proactive_avoidance_window_hours", 6) or 0)
    except (TypeError, ValueError):
        avoidance_hours = 6

    if avoidance_hours > 0:
        window_end = now + timedelta(hours=avoidance_hours)
        reminder_count = get_pending_reminder_count_in_window(
            account_id=account_id,
            start_at=now_text,
            end_at=_fmt(window_end),
        )
        if reminder_count > 0:
            return {"gate": "avoidance_user_reminder", "ok": False, "count": reminder_count}

        companion_count = get_pending_companion_followup_count_in_window(
            account_id=account_id,
            start_at=now_text,
            end_at=_fmt(window_end),
        )
        if companion_count > 0:
            return {"gate": "avoidance_companion_followup", "ok": False, "count": companion_count}

    sessions = list_sessions_for_account(account_id=account_id, limit=1)
    if not sessions:
        return {"gate": "session_missing", "ok": False}
    try:
        history_limit = int(getattr(settings, "proactive_account_check_context_messages", 12) or 12)
    except (TypeError, ValueError):
        history_limit = 12
    history = list_recent_messages(
        session_id=int(sessions[0]["id"]),
        limit=max(1, history_limit),
    )
    if not history:
        return {"gate": "no_recent_history", "ok": False}

    return {"gate": "ok", "ok": True}


def maybe_bootstrap_proactive_state(*, account_id: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Create the minimal proactive state needed by generation diagnostics, if missing."""
    if get_account(account_id=account_id) is None:
        return None
    if get_proactive_account_state(account_id=account_id) is not None:
        return None
    return upsert_proactive_account_state(
        account_id=account_id,
        enabled=True,
        next_scan_at=_fmt(now),
        metadata={"bootstrap_source": "diagnose_content_invitations"},
    )


def check_dispatch_policy(
    *,
    account_id: str,
    now: datetime,
    topic: Optional[str] = None,
) -> Dict[str, Any]:
    """调度层策略评估（quiet_hours、daily_limit、cooldown 等）。"""
    metadata = {"topic": topic} if topic else None
    decision = evaluate_outbound_policy(
        account_id=account_id,
        category=OutboundCategory.CONTENT_INVITATION,
        source="content_invitation",
        scheduled_at=now,
        now=now,
        metadata=metadata,
    )
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "counts": decision.counts,
        "next_allowed_at": decision.next_allowed_at,
    }


def invitation_summary(*, account_id: str) -> Dict[str, Any]:
    """现有 content_invitations 状态汇总。"""
    all_inv = list_content_invitations_for_account(account_id=account_id, limit=50)
    by_status: Dict[str, int] = {}
    for inv in all_inv:
        s = str(inv.get("status") or "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    latest = all_inv[0] if all_inv else None
    return {
        "total": len(all_inv),
        "by_status": by_status,
        "latest_id": latest["id"] if latest else None,
        "latest_status": latest["status"] if latest else None,
        "latest_topic": latest["topic"] if latest else None,
        "latest_updated_at": latest["updated_at"] if latest else None,
    }


def run_generation_check(*, account_id: str, now: datetime) -> Dict[str, Any]:
    """Run the real generator against the currently configured DB."""
    result = generate_content_invitation_candidate(account_id=account_id, now=now)
    compact = {
        "action": result.get("action"),
        "reason": result.get("reason"),
        "metadata": result.get("metadata") or {},
        "evaluated_at": result.get("evaluated_at") or result.get("at"),
    }
    compact["content_invitation"] = _compact_invitation(result.get("content_invitation"))
    return compact


def diagnose_account(
    *,
    account_id: str,
    now: datetime,
    sample_limit: int = 4,
    bootstrap_state: bool = False,
    run_generation: bool = False,
    require_today_chat_for_generation: bool = True,
) -> Dict[str, Any]:
    today_chat = today_chat_summary(
        account_id=account_id,
        today=now.date().isoformat(),
        sample_limit=sample_limit,
    )
    recent_history = recent_history_summary(
        account_id=account_id,
        sample_limit=sample_limit,
    )

    bootstrapped = None
    if bootstrap_state:
        bootstrapped = maybe_bootstrap_proactive_state(account_id=account_id, now=now)

    gen = check_generation_gates(account_id=account_id, now=now)
    inv = invitation_summary(account_id=account_id)
    dispatch = check_dispatch_policy(
        account_id=account_id,
        now=now,
        topic=inv.get("latest_topic"),
    )
    generation_run = None
    if run_generation:
        if require_today_chat_for_generation and int(today_chat.get("total") or 0) <= 0:
            generation_run = {
                "action": "no_op",
                "reason": "no_today_chat",
                "metadata": {},
                "evaluated_at": _fmt(now),
                "content_invitation": None,
            }
        else:
            generation_run = run_generation_check(account_id=account_id, now=now)
        inv = invitation_summary(account_id=account_id)
        dispatch = check_dispatch_policy(
            account_id=account_id,
            now=now,
            topic=inv.get("latest_topic"),
        )

    # 综合结论
    if not gen["ok"]:
        verdict = f"blocked_at_generation: {gen['gate']}"
    elif not dispatch["allowed"]:
        verdict = f"blocked_at_dispatch: {dispatch['reason']}"
    else:
        verdict = "ready_for_llm"

    return {
        "account_id": account_id,
        "verdict": verdict,
        "generation_gate": gen,
        "generation_run": generation_run,
        "dispatch_policy": dispatch,
        "invitations": inv,
        "today_chat": today_chat,
        "recent_history": recent_history,
        "bootstrapped_proactive_state": bool(bootstrapped),
    }


def _print_table(results: List[Dict[str, Any]], *, result_file: Optional[str] = None) -> None:
    quiet = is_quiet_hours(
        now=datetime.now(),
        start=getattr(settings, "proactive_quiet_hours_start", "22:00"),
        end=getattr(settings, "proactive_quiet_hours_end", "08:00"),
    )
    dispatch_enabled = getattr(settings, "reactivation_dispatch_enabled", False)

    print(f"\n{'='*72}")
    print(f"  内容邀请策略诊断  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  reactivation_dispatch_enabled={dispatch_enabled}  |  quiet_hours={quiet}")
    print(f"{'='*72}")

    for r in results:
        acc = r["account_id"]
        verdict = r["verdict"]
        inv = r["invitations"]
        dp = r["dispatch_policy"]
        today_chat = r.get("today_chat") or {}
        recent_history = r.get("recent_history") or {}

        symbol = "✓" if verdict == "ready_for_llm" else "✗"
        print(f"\n{symbol} {acc}")
        print(f"  verdict     : {verdict}")
        if r.get("bootstrapped_proactive_state"):
            print("  bootstrap   : created proactive_account_state(enabled=True)")
        print(f"  invitations : total={inv['total']}  by_status={inv['by_status']}")
        if inv["latest_id"]:
            print(f"  latest inv  : id={inv['latest_id']}  status={inv['latest_status']}  topic={inv['latest_topic']}  updated={inv['latest_updated_at']}")
        print(
            "  today chat  : "
            f"date={today_chat.get('date')}  total={today_chat.get('total', 0)}  "
            f"by_role={today_chat.get('by_role', {})}"
        )
        for item in today_chat.get("samples") or []:
            print(
                "    today     : "
                f"[{item.get('created_at')}] {item.get('role')}: {item.get('content')}"
            )
        print(
            "  gen history : "
            f"session={recent_history.get('session_id')}  "
            f"context_messages={recent_history.get('total_sampled', 0)}"
        )
        for item in recent_history.get("samples") or []:
            print(f"    history   : {item.get('role')}: {item.get('content')}")
        gate = r.get("generation_gate") or {}
        if gate and not gate.get("ok"):
            extra = {k: v for k, v in gate.items() if k not in {"gate", "ok"}}
            detail = f"  generation  : blocked={gate.get('gate')}"
            if extra:
                detail += f"  detail={extra}"
            print(detail)
        elif gate.get("ok"):
            print("  generation  : gate=ok")
        run = r.get("generation_run")
        if run:
            detail = f"  llm probe   : action={run.get('action')}"
            if run.get("reason"):
                detail += f"  reason={run.get('reason')}"
            print(detail)
            created = run.get("content_invitation")
            if created:
                print(
                    "    candidate : "
                    f"id={created.get('id')}  topic={created.get('topic')}  "
                    f"titles={created.get('title_count')}  scheduled={created.get('scheduled_at')}"
                )
                print(f"    invite    : {created.get('invitation_text')}")
                for title in (created.get("titles") or [])[:5]:
                    print(f"    title     : {title.get('title')}  ({title.get('source_name')})")
        if not dp["allowed"]:
            detail = f"  dispatch    : blocked={dp['reason']}"
            if dp.get("next_allowed_at"):
                detail += f"  next={dp['next_allowed_at']}"
            if dp.get("counts"):
                detail += f"  counts={dp['counts']}"
            print(detail)

    # 汇总
    verdicts: Dict[str, int] = {}
    for r in results:
        v = r["verdict"]
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"\n{'─'*72}")
    print(f"  汇总（共 {len(results)} 个账号）：")
    for v, cnt in sorted(verdicts.items(), key=lambda x: -x[1]):
        print(f"    {cnt:3d}  {v}")
    if result_file:
        print(f"\n  LLM probe result file: {result_file}")
    print(f"{'='*72}\n")


def write_result_file(results: List[Dict[str, Any]], *, output: Optional[str] = None) -> str:
    """Persist diagnostic results outside the production DB for later review."""
    if output:
        path = Path(output)
    else:
        path = Path(tempfile.gettempdir()) / (
            "ai4all_content_invitation_diagnosis_"
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


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断内容邀请策略触发情况")
    parser.add_argument("--account", help="只诊断指定账号")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--sample-limit", type=int, default=4, help="每个账号展示的聊天样本条数")
    parser.add_argument(
        "--bootstrap-state",
        action="store_true",
        help="仅在 --run-generation 临时数据库中，为缺失 proactive_account_state 的账号创建最小启用状态；--run-generation 默认启用",
    )
    parser.add_argument(
        "--strict-state",
        action="store_true",
        help="--run-generation 时不临时补 proactive_account_state，严格复现生产 gate",
    )
    parser.add_argument(
        "--run-generation",
        action="store_true",
        help="调用真实内容邀请生成器；会发起 LLM 请求，但只写临时数据库和结果 JSON",
    )
    parser.add_argument(
        "--include-no-today-chat",
        action="store_true",
        help="即使账号今天没有聊天记录，也执行 LLM 生成探测",
    )
    parser.add_argument(
        "--output",
        help="--run-generation 的 JSON 结果文件路径；默认写入 /tmp",
    )
    args = parser.parse_args()

    now = datetime.now()

    if args.account:
        accounts = [{"id": args.account}]
    else:
        accounts = [a for a in list_accounts() if a.get("status") == "active"]

    result_file = None

    def collect_results() -> List[Dict[str, Any]]:
        items = []
        for a in accounts:
            account_id = a["id"]
            try:
                items.append(
                    diagnose_account(
                        account_id=account_id,
                        now=now,
                        sample_limit=args.sample_limit,
                        bootstrap_state=(
                            args.run_generation
                            and (args.bootstrap_state or not args.strict_state)
                        ),
                        run_generation=args.run_generation,
                        require_today_chat_for_generation=not args.include_no_today_chat,
                    )
                )
            except Exception as exc:
                items.append({
                    "account_id": account_id,
                    "verdict": f"error: {exc}",
                    "generation_gate": {},
                    "generation_run": None,
                    "dispatch_policy": {},
                    "invitations": {},
                    "today_chat": {},
                    "recent_history": {},
                    "bootstrapped_proactive_state": False,
                })
        return items

    if args.run_generation:
        with temporary_database_copy() as temp_db_path:
            results = collect_results()
            result_file = write_result_file(results, output=args.output)
            for item in results:
                item["temporary_database_path"] = temp_db_path
    else:
        results = collect_results()

    if args.as_json:
        payload = {"result_file": result_file, "results": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(results, result_file=result_file)


if __name__ == "__main__":
    main()
