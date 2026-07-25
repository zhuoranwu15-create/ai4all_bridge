#!/usr/bin/env python3
"""按发布检查点推进 MP-01～MP-06 migration；必须在全部 writer drain 后运行。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_EXPECTED_PREVIOUS = {
    38: 36,
    39: 38,
    40: 39,
    41: 40,
    42: 41,
    43: 42,
    44: 43,
    45: 44,
    46: 45,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--through",
        type=int,
        required=True,
        choices=tuple(_EXPECTED_PREVIOUS),
        help="本次允许推进到的 migration 版本",
    )
    args = parser.parse_args()

    from app.db._backend import is_postgres
    from app.db._core import migrate_db_through

    if not is_postgres():
        parser.error("Phase 1 checkpoint migration requires PostgreSQL DATABASE_URL")

    result = migrate_db_through(
        target_version=args.through,
        expected_current_version=_EXPECTED_PREVIOUS[args.through],
    )
    print(
        "migration_result=PASS "
        f"before={result['before']} after={result['after']} target={args.through}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
