"""初始化 Plum 本地固定测试账号和演示目录。"""
from __future__ import annotations

import json

from app.config import settings
from app.db import init_db
from app.products.plum.infrastructure.repository import seed_plum_dev


def main() -> None:
    env = str(settings.app_env or "").strip().lower()
    if env not in {"local", "development", "test"} or not settings.plum_dev_mode:
        raise SystemExit("Plum dev seed is disabled outside local/development/test")
    init_db()
    print(json.dumps(seed_plum_dev(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
