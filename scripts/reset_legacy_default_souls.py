#!/usr/bin/env python
"""Reset legacy default SOUL.md files to the current blank preset.

Default mode is dry-run: print every file that would change and its unified
diff. Add --apply only after reviewing the diff.

Usage:
    .venv/bin/python scripts/reset_legacy_default_souls.py
    .venv/bin/python scripts/reset_legacy_default_souls.py --account aid_532549594
    .venv/bin/python scripts/reset_legacy_default_souls.py --include-missing
    .venv/bin/python scripts/reset_legacy_default_souls.py --apply
"""
import argparse
import difflib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.db import list_accounts
from app.user_profiles import apply_soul_preset, context_file_path, read_context_file, render_soul_preset


OLD_DEFAULT_SOUL_BODY = (
    "你是这个微信账号的个人 AI 陪伴与生活助理。回应要自然、温和、简洁，"
    "优先提供情绪陪伴、日常建议和生活协助。"
)
OLD_DEFAULT_SOUL_FILE = f"# SOUL\n\n{OLD_DEFAULT_SOUL_BODY}"


@dataclass(frozen=True)
class SoulResetCandidate:
    account_id: str
    path: Path
    reason: str
    before: str
    after: str


def _normalize(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _body_without_soul_heading(text: str) -> str:
    lines = _normalize(text).splitlines()
    if lines and lines[0].strip() == "# SOUL":
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines).strip()


def reset_reason_for_soul_text(text: str) -> Optional[str]:
    """Return reset reason for SOUL.md text, or None when it is custom/current."""
    normalized = _normalize(text)
    body = _body_without_soul_heading(text)
    if not normalized or not body:
        return "empty"
    if normalized == OLD_DEFAULT_SOUL_FILE or body == OLD_DEFAULT_SOUL_BODY:
        return "legacy_default"
    return None


def plan_account_reset(*, account_id: str, include_missing: bool = False) -> Optional[SoulResetCandidate]:
    """Build a reset candidate for one account without writing files."""
    path = context_file_path(account_id, "SOUL.md")  # 逻辑路径，仅供展示/diff 标题
    before = read_context_file(account_id, "SOUL.md")  # P2 后从 storage 读，缺失返回 None
    if before is None:
        if not include_missing:
            return None
        before = ""
        reason = "missing"
    else:
        reason = reset_reason_for_soul_text(before)
        if reason is None:
            return None

    after = render_soul_preset(account_id=account_id, preset_name="blank")
    if _normalize(before) == _normalize(after):
        return None
    return SoulResetCandidate(
        account_id=account_id,
        path=path,
        reason=reason,
        before=before,
        after=after,
    )


def collect_reset_candidates(
    *,
    account_ids: Iterable[str],
    include_missing: bool = False,
    limit: Optional[int] = None,
) -> List[SoulResetCandidate]:
    """Collect reset candidates for accounts without writing files."""
    candidates: List[SoulResetCandidate] = []
    for account_id in account_ids:
        candidate = plan_account_reset(
            account_id=account_id,
            include_missing=include_missing,
        )
        if candidate is None:
            continue
        candidates.append(candidate)
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


def render_candidate_diff(candidate: SoulResetCandidate) -> str:
    """Render a unified diff for one planned SOUL.md reset."""
    before = candidate.before
    after = candidate.after
    if before and not before.endswith("\n"):
        before += "\n"
    if after and not after.endswith("\n"):
        after += "\n"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{candidate.path} (before)",
            tofile=f"{candidate.path} (after)",
        )
    )


def _account_ids_from_db(account_filter: Optional[str]) -> List[str]:
    if account_filter:
        return [account_filter]
    return [str(account["id"]) for account in list_accounts()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset old default SOUL.md files to blank.md")
    parser.add_argument("--account", default=None, help="只处理该 account_id（默认扫描 DB 账号）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则仅输出 diff 预演")
    parser.add_argument("--include-missing", action="store_true", help="也重置缺失 SOUL.md 的账号")
    parser.add_argument("--limit", type=int, default=None, help="最多处理的候选账号数")
    args = parser.parse_args()

    account_ids = _account_ids_from_db(args.account)
    candidates = collect_reset_candidates(
        account_ids=account_ids,
        include_missing=args.include_missing,
        limit=args.limit,
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"mode={mode}")
    print(f"database={settings.database_path}")
    print(f"user_profiles_dir={settings.user_profiles_dir}")
    print(f"account_filter={args.account or 'all-db-accounts'}")
    print(f"include_missing={args.include_missing}")
    print(f"scanned_accounts={len(account_ids)}")
    print(f"candidates={len(candidates)}")

    if not candidates:
        print("\nNo SOUL.md files matched empty/legacy-default reset rules.")
        return 0

    for index, candidate in enumerate(candidates, start=1):
        print("\n" + "=" * 80)
        print(
            f"[{index}/{len(candidates)}] account={candidate.account_id} "
            f"reason={candidate.reason} path={candidate.path}"
        )
        print("-" * 80)
        print(render_candidate_diff(candidate), end="")
        if args.apply:
            apply_soul_preset(candidate.account_id, "blank")
            print(f"\nAPPLIED account={candidate.account_id} path={candidate.path}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply after confirming the diffs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
