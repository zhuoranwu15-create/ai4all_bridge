"""初始化 Plum 本地固定测试账号和演示目录。"""
from __future__ import annotations

import json

from app.config import settings
from app.db import init_db
from app.products.plum.infrastructure.repository import seed_plum_dev
from scripts.init_local_postgres import _validated_conninfo


def main() -> None:
    env = str(settings.app_env or "").strip().lower()
    if env not in {"local", "development", "test"} or not settings.plum_dev_mode:
        raise SystemExit("Plum dev seed is disabled outside local/development/test")
    try:
        _validated_conninfo(settings.database_url, "ai4all_plum_dev")
    except ValueError as exc:
        raise SystemExit(f"Plum dev seed requires the loopback ai4all_plum_dev database: {exc}")
    init_db()
    print(json.dumps(seed_plum_dev(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
