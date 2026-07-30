"""孤儿媒体回收（v1.5 S1 / D-10）。

上传成功只意味着"字节已存下"：资产处于 ``pending``，客户端可能最终没把它发出去（发送前
取消、切走、崩溃）。这些资产在 ``expires_at``（默认 2 小时）后连行带文件清掉，否则磁盘只增不减。

**删除顺序必须是"先删行、再删文件"**：`DELETE ... AND status = 'pending'` 是原子的，命中
才说明这份资产此刻仍无人引用、由本次回收独占；若反过来先删文件，一个并发的发送事务刚把它翻成
``referenced``，就会留下"消息里有图、磁盘没文件"的坏读。

按 tick 调用即可：函数自身按 ``media_reclaim_interval_seconds`` 做节流（同 hot topic pool 的
做法），未到点立刻返回 ``skipped``，不产生任何 DB 开销。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from app.config import settings
from app.platform.media.assets import delete_media_file
from app.platform.media.persistence import (
    delete_media_asset_row,
    list_expired_pending_media_assets,
)

logger = logging.getLogger("ai4all.media.reclaim")

# 进程内上次执行时刻（monotonic 秒）。多 worker 各扫一次没有正确性问题：删除本身按
# `status='pending'` 原子判定，重复扫描只会多一次空转。
_last_run_at: Optional[float] = None


def reset_reclaim_throttle() -> None:
    """清掉节流状态。供测试使用，生产不调用。"""
    global _last_run_at
    _last_run_at = None


def _interval_seconds() -> float:
    return max(float(getattr(settings, "media_reclaim_interval_seconds", 3600.0) or 3600.0), 1.0)


def reclaim_orphan_media_batch(
    *,
    now: Optional[str] = None,
    limit: int = 200,
    force: bool = False,
    _monotonic: Optional[float] = None,
) -> Dict[str, Any]:
    """回收一批过期未引用的媒体资产。

    :param now: 判定过期的基准时间（库内北京时间字符串），默认当前时间。
    :param limit: 单轮最多处理多少条，避免一次扫太多阻塞 scheduler 线程。
    :param force: 跳过节流，供 admin/测试立刻跑一轮。
    :returns: ``{"status", "scanned", "deleted_rows", "deleted_files", "skipped_referenced",
        "file_errors"}``；``status="skipped"`` 表示本 tick 被节流跳过。
    """
    global _last_run_at
    clock = _monotonic if _monotonic is not None else time.monotonic()
    if not force and _last_run_at is not None and clock - _last_run_at < _interval_seconds():
        return {"status": "skipped", "scanned": 0, "deleted_rows": 0, "deleted_files": 0}
    _last_run_at = clock

    candidates = list_expired_pending_media_assets(limit=limit, now=now)
    deleted_rows = 0
    deleted_files = 0
    skipped_referenced = 0
    file_errors = 0
    for asset in candidates:
        media_id = str(asset.get("id") or "")
        if not delete_media_asset_row(media_id=media_id, only_pending=True):
            # 扫描与删除之间被发送事务引用了：这份资产已经不是孤儿，文件必须留着。
            skipped_referenced += 1
            continue
        deleted_rows += 1
        try:
            if delete_media_file(str(asset.get("storage_path") or "")):
                deleted_files += 1
        except Exception as err:  # noqa: BLE001 — 删文件失败不能中断整批回收
            file_errors += 1
            logger.warning(
                "media reclaim failed to delete file media_id=%s error_type=%s",
                media_id,
                type(err).__name__,
            )
    if deleted_rows or skipped_referenced or file_errors:
        logger.info(
            "media reclaim done scanned=%d deleted_rows=%d deleted_files=%d "
            "skipped_referenced=%d file_errors=%d",
            len(candidates),
            deleted_rows,
            deleted_files,
            skipped_referenced,
            file_errors,
        )
    return {
        "status": "ok",
        "scanned": len(candidates),
        "deleted_rows": deleted_rows,
        "deleted_files": deleted_files,
        "skipped_referenced": skipped_referenced,
        "file_errors": file_errors,
    }


__all__ = ["reclaim_orphan_media_batch", "reset_reclaim_throttle"]
