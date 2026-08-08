"""Create one Plum public-test invite and seed the shared catalog."""
from __future__ import annotations

import argparse
import json

from app.db import init_db
from app.products.plum.infrastructure.repository import (
    create_plum_access_invite,
    seed_plum_catalog,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    parser.add_argument("--expires-days", type=int, default=30)
    args = parser.parse_args()
    init_db()
    seed_plum_catalog()
    invite = create_plum_access_invite(
        label=args.label,
        expires_days=args.expires_days,
    )
    print(json.dumps(invite, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
