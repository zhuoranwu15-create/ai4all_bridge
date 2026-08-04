"""图片机审命中后的鸣蝉侧下架（v1.5 S4 / D-7）。

平台批处理（:mod:`app.platform.media.moderation`）只负责"送审 + 写结论"，**不认识动态和会话**
——它在 `app/platform` 下，按分层不能 import `app.products.*`。所以"命中红线之后做什么"由这里
注入：Feed 图走既有终态下架，会话图只记日志。

放 `jobs/` 而不是 `jobs/world_content/`：后者是 AI 内容**生成**的调度域，这里是审核回调，
两件事共用一个包只会让 world_content 的边界变模糊。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import app.db as world_db
from app.platform.media.moderation import review_pending_media_batch
from app.platform.moderation.models import MachineReviewResult
from app.time_utils import beijing_now_str

logger = logging.getLogger("ai4all.mingchan.media_moderation")

# 机审下架落在 ``terminal_reason`` 上，与主人自删（``owner_deleted``）、隐藏 AI 动态
# （``owner_hidden``）、人工纠正（``admin_correction``）区分开。
TERMINAL_REASON_MODERATION = "moderation"
_MEDIA_PATH_PREFIX = "/api/v1/products/mingchan/media"


def retire_content_for_rejected_media(
    asset: Dict[str, Any], result: MachineReviewResult
) -> None:
    """命中红线的图片：Feed 动态下架，会话图只记录。

    Feed 走 :func:`retire_feed_post_with_outbox`——与主人自删同一条终态写路径，主人 Feed 与
    访客 Feed 都只筛 ``published``，一次写入对双方同时生效，并原子追加 deleted outbox 通知端上。
    重放安全（"首次写入者胜出"），所以批处理宁可重复调用一次也不会丢下架。

    会话图**刻意不撤回**：v1.5 不做消息撤回（D-7 已知敞口），审核结论已经落在
    ``media_assets.moderation_status``，供事后人工处置与 v1.6 撤回能力补做。
    """
    media_id = str(asset.get("id") or "")
    owner_platform_user_id = str(asset.get("owner_platform_user_id") or "")
    located = world_db.find_post_owner_by_media_id(media_id=media_id)
    if located is None:
        # 不在 Feed 上：会话图，或还没被任何内容引用（后者会被 D-10 回收）。
        logger.warning(
            "media moderation rejected non-feed image media_id=%s owner=%s level=%s "
            "categories=%s (no takedown in v1.5)",
            media_id,
            owner_platform_user_id,
            result.level,
            result.categories,
        )
        return
    post_id = str(located.get("post_id") or "")
    _post, changed = world_db.retire_feed_post_with_outbox(
        owner_platform_user_id=str(located.get("owner_platform_user_id") or ""),
        post_id=post_id,
        reason_code=TERMINAL_REASON_MODERATION,
        deleted_at=beijing_now_str(),
    )
    logger.warning(
        "media moderation retired feed post post_id=%s media_id=%s level=%s changed=%s",
        post_id,
        media_id,
        result.level,
        changed,
    )


def review_pending_media_job(
    *, limit: Optional[int] = None, force: bool = False
) -> Dict[str, Any]:
    """scheduler 注入用：把鸣蝉下架回调绑到平台批处理上。"""
    return review_pending_media_batch(
        limit=limit,
        force=force,
        on_rejected=retire_content_for_rejected_media,
        media_path_prefix=_MEDIA_PATH_PREFIX,
    )


__all__ = [
    "TERMINAL_REASON_MODERATION",
    "retire_content_for_rejected_media",
    "review_pending_media_job",
]
