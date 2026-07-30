"""图片机审批处理（v1.5 S4 / D-7 先发后审、仅红线）。

App 侧的图片**发出去就可见**，机审在后台补做。这里是那个"补做"的批处理：扫
``media_assets.moderation_status = 'pending'`` 的图片，逐条送阿里云，结论写回终态，命中红线
的交给注入的 ``on_rejected`` 去下架。

三个与常规审核链路不同的决定，都是被现状逼出来的：

1. **不建 ``content_moderation_tasks``**。那张表的 ``account_id`` 带 ``FOREIGN KEY → accounts``，
   而 App 侧内容属于 ``platform_users``（没有 accounts 行）。所以 App 媒体的状态机就是
   ``media_assets.moderation_status`` 本身，直接调 :func:`review_image_url`，不进审核队列。
2. **给阿里云的是我们自己签的短 TTL 读 URL**（owner ``pu:`` scope）。阿里云
   ``ImageModeration`` 只收 ``imageUrl``（公网可取）或 OSS 对象，**不收字节流**，所以必须
   给它一个能回源取到图的绝对地址；复用主人自己的 scope 意味着不新增任何鉴权面。
   这也让 ``MEDIA_PUBLIC_BASE_URL`` 成为硬部署约束：媒体读端点不公网可达，机审就跑不起来。
3. **只对红线（``block`` / ``escalate``）动手**。``review`` 档放过并记日志——阿里云 review
   档误报率不低，先发后审下"删掉用户已经看见的内容"的代价远高于让人工事后处理。

按 tick 调用即可：函数自身按 ``media_moderation_interval_seconds`` 节流（同 D-10 回收的做法）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.platform.media.access import owner_scope, owner_ttl_seconds, sign_media_url
from app.platform.media.persistence import (
    MODERATION_STATUS_PASSED,
    MODERATION_STATUS_REJECTED,
    MODERATION_STATUS_SKIPPED,
    bump_media_moderation_attempts,
    list_pending_moderation_media_assets,
    update_media_moderation_status,
)
from app.platform.moderation.image_review import image_review_configured, review_image_url
from app.platform.moderation.models import MachineReviewResult

logger = logging.getLogger("ai4all.media.moderation")

# 命中红线的等级（``review`` 不在内，见模块 docstring 决定 3）。
_REJECT_LEVELS = {"block", "escalate"}

# 单份资产最多试几次。达上限后按先发后审的口径 **fail-open 记 skipped 放过**，而不是
# 当成命中红线删掉用户内容——机审自己坏了不是用户的错。
MAX_MODERATION_ATTEMPTS = 3

# 下架回调：``(asset_row, review_result) -> None``。抛异常表示这次没下架成功，本轮不结案，
# 资产留在 pending 下一轮重试。
OnRejected = Callable[[Dict[str, Any], MachineReviewResult], None]

# 进程内上次执行时刻（monotonic 秒）。多进程各扫一次不影响正确性：结案是
# ``pending -> 终态`` 的原子写，重复扫描最多多打一次云接口。
_last_run_at: Optional[float] = None


def reset_media_moderation_throttle() -> None:
    """清掉节流状态。供测试使用，生产不调用。"""
    global _last_run_at
    _last_run_at = None


def _interval_seconds() -> float:
    return max(
        float(getattr(settings, "media_moderation_interval_seconds", 60.0) or 60.0), 1.0
    )


def _batch_size() -> int:
    return max(int(getattr(settings, "media_moderation_batch_size", 50) or 50), 1)


def public_base_url() -> str:
    """媒体读端点的公网基址（去掉尾斜杠）；留空即图片机审不可用。"""
    return str(getattr(settings, "media_public_base_url", "") or "").strip().rstrip("/")


def media_moderation_ready() -> bool:
    """机审能不能真正跑：开关/凭证/场景码齐全 **且** 配了公网基址。"""
    return image_review_configured() and bool(public_base_url())


def _public_media_url(asset: Dict[str, Any]) -> str:
    """给这份资产签一个阿里云可回源取到的绝对 URL（主人 scope、短 TTL）。"""
    grant = sign_media_url(
        media_id=str(asset.get("id") or ""),
        scope=owner_scope(str(asset.get("owner_platform_user_id") or "")),
        ttl_seconds=owner_ttl_seconds(),
    )
    return f"{public_base_url()}{grant.url}"


def review_pending_media_batch(
    *,
    limit: Optional[int] = None,
    force: bool = False,
    on_rejected: Optional[OnRejected] = None,
    _monotonic: Optional[float] = None,
) -> Dict[str, Any]:
    """过一批待审图片。

    :param limit: 单轮最多处理多少条，默认取 ``media_moderation_batch_size``。
    :param force: 跳过节流，供 admin/测试立刻跑一轮。
    :param on_rejected: 命中红线时的下架回调（产品侧注入）；不传则只写状态不下架。
    :returns: ``{"status", "scanned", "passed", "rejected", "errors", "exhausted",
        "takedown_errors"}``。``status="disabled"`` 表示机审未配置（不做任何 DB 读），
        ``status="skipped"`` 表示本 tick 被节流跳过。
    """
    global _last_run_at
    empty = {
        "scanned": 0,
        "passed": 0,
        "rejected": 0,
        "errors": 0,
        "exhausted": 0,
        "takedown_errors": 0,
    }
    if not media_moderation_ready():
        # 未配置时连扫描都不做：资产此时恒为 skipped，队列本就是空的。
        return {"status": "disabled", **empty}
    clock = _monotonic if _monotonic is not None else time.monotonic()
    if not force and _last_run_at is not None and clock - _last_run_at < _interval_seconds():
        return {"status": "skipped", **empty}
    _last_run_at = clock

    candidates = list_pending_moderation_media_assets(limit=limit or _batch_size())
    counters = dict(empty)
    counters["scanned"] = len(candidates)
    for asset in candidates:
        media_id = str(asset.get("id") or "")
        attempts = int(asset.get("moderation_attempts") or 0)
        if attempts >= MAX_MODERATION_ATTEMPTS:
            # 重试用尽：fail-open 放过并结案，避免这行永远留在待审索引里。
            if update_media_moderation_status(
                media_id=media_id, status=MODERATION_STATUS_SKIPPED
            ):
                counters["exhausted"] += 1
                logger.warning(
                    "media moderation gave up media_id=%s attempts=%d -> skipped",
                    media_id,
                    attempts,
                )
            continue
        try:
            bump_media_moderation_attempts(media_id=media_id)
            image_url = _public_media_url(asset)
        except Exception as err:  # noqa: BLE001 — 单条失败不能中断整批
            counters["errors"] += 1
            logger.warning(
                "media moderation could not prepare media_id=%s error_type=%s",
                media_id,
                type(err).__name__,
            )
            continue

        result = review_image_url(
            image_url=image_url,
            data_id=media_id,
            user_id=str(asset.get("owner_platform_user_id") or ""),
        )
        if result.error:
            # 留在 pending 等下一轮；attempts 已经加过，坏配置最多再试 MAX-1 次。
            counters["errors"] += 1
            logger.warning(
                "media moderation call failed media_id=%s error=%s attempts=%d",
                media_id,
                result.error,
                attempts + 1,
            )
            continue

        if result.level in _REJECT_LEVELS:
            if on_rejected is not None:
                try:
                    on_rejected(asset, result)
                except Exception as err:  # noqa: BLE001 — 下架失败则不结案，下轮重试
                    counters["takedown_errors"] += 1
                    logger.exception(
                        "media moderation takedown failed media_id=%s error_type=%s",
                        media_id,
                        type(err).__name__,
                    )
                    continue
            if update_media_moderation_status(
                media_id=media_id, status=MODERATION_STATUS_REJECTED
            ):
                counters["rejected"] += 1
                logger.warning(
                    "media moderation rejected media_id=%s level=%s categories=%s",
                    media_id,
                    result.level,
                    result.categories,
                )
            continue

        if update_media_moderation_status(
            media_id=media_id, status=MODERATION_STATUS_PASSED
        ):
            counters["passed"] += 1
            if result.level == "review":
                # 放过但留痕：运营可据此事后人工处置（D-7 只对红线动手）。
                logger.info(
                    "media moderation passed with review level media_id=%s categories=%s",
                    media_id,
                    result.categories,
                )
    if counters["scanned"]:
        logger.info(
            "media moderation done scanned=%d passed=%d rejected=%d errors=%d "
            "exhausted=%d takedown_errors=%d",
            counters["scanned"],
            counters["passed"],
            counters["rejected"],
            counters["errors"],
            counters["exhausted"],
            counters["takedown_errors"],
        )
    return {"status": "ok", **counters}


__all__ = [
    "MAX_MODERATION_ATTEMPTS",
    "OnRejected",
    "media_moderation_ready",
    "public_base_url",
    "reset_media_moderation_throttle",
    "review_pending_media_batch",
]
