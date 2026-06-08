"""入口：增量刷新基础表（dim_/fct_）。

用法：
  python -m nearline.run_etl
  python nearline/run_etl.py --source-db /path/to/ai4all.sqlite3
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nearline.analytics.warehouse import etl  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="nearline ETL：增量刷新 dim_/fct_")
    parser.add_argument("--source-db", default=None, help="覆盖操作库路径（默认 data/ai4all.sqlite3）")
    args = parser.parse_args(argv)
    stats = etl.refresh_all(args.source_db)
    print(f"[etl] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
