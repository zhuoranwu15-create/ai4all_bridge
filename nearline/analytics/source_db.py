"""只读连接操作库（operational source）。

nearline 与主 app 解耦：默认读仓库根 data/ai4all.sqlite3（标准库），
可用 connect_source(override) 或环境变量 NEARLINE_SOURCE_DB 覆盖。
一律以 mode=ro 打开，禁止对操作库写入。
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

# nearline/analytics/source_db.py -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DB = REPO_ROOT / "data" / "ai4all.sqlite3"


def source_db_path(override: Optional[str] = None) -> Path:
    """解析操作库路径：显式 override > 环境变量 > 默认标准库。"""
    if override:
        return Path(override)
    env = os.environ.get("NEARLINE_SOURCE_DB")
    if env:
        return Path(env)
    return DEFAULT_SOURCE_DB


def connect_source(override: Optional[str] = None) -> sqlite3.Connection:
    """返回只读连接（mode=ro）。库不存在直接抛错，避免静默空跑。"""
    path = source_db_path(override)
    if not path.exists():
        raise FileNotFoundError(f"operational source db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
