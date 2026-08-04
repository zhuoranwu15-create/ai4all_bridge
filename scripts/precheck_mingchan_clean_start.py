#!/usr/bin/env python3
"""只读检查鸣蝉能否从空产品数据开始；输出仅含计数和布尔结论。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cleanup_legacy_app_test_data import (  # noqa: E402
    build_cleanup_plan,
    configure_database_url_override,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="鸣蝉 clean-start 只读预检")
    parser.add_argument(
        "--database-url",
        default=None,
        help="可选覆盖 PostgreSQL DATABASE_URL；SQLite 临时库请设置 DATABASE_PATH",
    )
    args = parser.parse_args()
    try:
        configure_database_url_override(args.database_url)
    except ValueError as err:
        parser.error(str(err))
    report = build_cleanup_plan()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["safe_to_apply"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
