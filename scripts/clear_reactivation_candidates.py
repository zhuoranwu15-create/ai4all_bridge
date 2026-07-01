"""One-time baseline cleanup: drop stale reactivation candidates.

Context: before the unified-reactivation dispatch fix, content_invitation
candidates leaked out through a legacy sweep. The reactivation_candidate
mirrors that were queued under that buggy regime (scheduled for an already-past
slot) must be cleared so that enabling reactivation dispatch later never replays
content already delivered today.

This only touches `proactive_account_state.metadata.reactivation_candidate`
(via clear_reactivation_candidate, which scopes the write to that key). It does
NOT send anything and does NOT touch delivered content_invitations rows.

Default is dry-run; pass --apply to actually clear.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.proactive.store.candidates import clear_reactivation_candidate, get_reactivation_candidate_from_metadata  # noqa: E402


def _accounts_with_candidate():
    rows = []
    with connect() as conn:
        for row in conn.execute(
            "SELECT account_id, metadata_json FROM proactive_account_state"
        ).fetchall():
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            candidate = get_reactivation_candidate_from_metadata(metadata)
            if candidate is not None:
                rows.append((row["account_id"], candidate))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually clear (default: dry-run)")
    args = parser.parse_args()

    now = datetime.now()
    targets = _accounts_with_candidate()
    if not targets:
        print("no reactivation candidates found; nothing to clear")
        return

    by_type: dict[str, int] = {}
    for account_id, candidate in targets:
        ctype = candidate.get("type", "?")
        by_type[ctype] = by_type.get(ctype, 0) + 1
        text = (candidate.get("text") or "")[:40]
        action = "CLEAR" if args.apply else "DRY-RUN"
        print(f"[{action}] {account_id}  type={ctype}  sched={candidate.get('scheduled_at')}  text={text}")
        if args.apply:
            clear_reactivation_candidate(
                account_id=account_id,
                reason="phase_b_baseline_cleanup",
                now=now,
            )

    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    verb = "cleared" if args.apply else "would clear"
    print(f"\n{verb} {len(targets)} candidate(s): {summary}")
    if not args.apply:
        print("re-run with --apply to actually clear")


if __name__ == "__main__":
    main()
