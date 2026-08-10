"""鸣蝉账号注销执行流水。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from app.bootstrap.product_registry import MINGCHAN_APP_ID
from app.db._core import _new_id, _tx, connect

DELETION_REASON_CODES = (
    "not_useful",
    "privacy_concern",
    "too_expensive",
    "switching",
    "other",
)


def record_deletion_execution(
    *,
    platform_user_id: str,
    reason_code: Optional[str],
    purge_stats: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """记录一次已完成的鸣蝉注销；每次重新注册后的注销都保留独立流水。"""

    if reason_code is not None and reason_code not in DELETION_REASON_CODES:
        raise ValueError("invalid deletion reason_code")
    current = now.strftime("%Y-%m-%d %H:%M:%S")
    request_id = _new_id("adr")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO account_deletion_requests(
                id, platform_user_id, app_id, status, reason_code,
                executed_at, purge_stats_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'executed', ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                platform_user_id,
                MINGCHAN_APP_ID,
                reason_code,
                current,
                json.dumps(purge_stats or {}, ensure_ascii=False, sort_keys=True),
                current,
                current,
            ),
        )
        row = conn.execute(
            "SELECT * FROM account_deletion_requests WHERE id = ? AND app_id = ?",
            (request_id, MINGCHAN_APP_ID),
        ).fetchone()
    if row is None:
        raise RuntimeError("deletion record disappeared after insert")
    return dict(row)


def get_last_deletion_record(
    *, platform_user_id: str
) -> Optional[Dict[str, Any]]:
    """读取真人最近一条鸣蝉注销流水；不返回其他产品记录。"""

    with _tx(None) as tx:
        row = tx.execute(
            """
            SELECT * FROM account_deletion_requests
            WHERE platform_user_id = ? AND app_id = ?
            ORDER BY executed_at DESC, id DESC
            LIMIT 1
            """,
            (platform_user_id, MINGCHAN_APP_ID),
        ).fetchone()
    return dict(row) if row else None


__all__ = [
    "DELETION_REASON_CODES",
    "get_last_deletion_record",
    "record_deletion_execution",
]
