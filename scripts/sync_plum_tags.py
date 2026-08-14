"""Validate and synchronize the offline Plum Tag configuration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import init_db
from app.products.plum.infrastructure.tags import (
    DEFAULT_TAG_CONFIG_PATH,
    sync_tag_config,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_TAG_CONFIG_PATH)
    args = parser.parse_args()
    init_db()
    result = sync_tag_config(args.file)
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
