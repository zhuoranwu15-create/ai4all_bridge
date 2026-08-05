"""朝夕微信产品 scheduler 生命周期。"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import settings
from app.products.zhaoxi.jobs.dreaming.scheduler import (
    start_dreaming_scheduler,
    stop_dreaming_scheduler,
)
from app.products.zhaoxi.jobs.user_meta.scheduler import (
    start_user_meta_scheduler,
    stop_user_meta_scheduler,
)
from app.products.zhaoxi.proactive.orchestration.scheduler import (
    start_proactive_scheduler,
    stop_proactive_scheduler,
)

logger = logging.getLogger("ai4all.products.zhaoxi.lifecycle")


def install_lifecycle(app: FastAPI) -> None:
    """注册朝夕产品启动和关停事件。"""

    @app.on_event("startup")
    def validate_product_registries() -> None:
        from app.products.zhaoxi.proactive.contract.categories import (
            validate_category_registry,
        )

        validate_category_registry()

    @app.on_event("startup")
    async def startup_proactive_scheduler() -> None:
        if not getattr(settings, "proactive_scheduler_enabled", False):
            return
        if not settings.has_central_role:
            logger.warning(
                "proactive scheduler skipped: AI4ALL_ROLE=%r 非 central/standalone",
                settings.ai4all_role,
            )
            return
        scheduler = start_proactive_scheduler(
            interval_seconds=settings.proactive_scheduler_interval_seconds,
            batch_size=settings.proactive_scheduler_batch_size,
            bypass_quiet_hours=settings.proactive_scheduler_bypass_quiet_hours,
            planning_interval_seconds=settings.proactive_planning_interval_seconds,
            node_id=settings.node_id or None,
        )
        logger.info("proactive scheduler started: %s", scheduler.status())

    @app.on_event("startup")
    async def startup_dreaming_scheduler() -> None:
        if not getattr(settings, "dreaming_scheduler_enabled", False):
            return
        daily_scan_node_id = (
            None if settings.has_central_role else (settings.node_id or None)
        )
        scheduler = start_dreaming_scheduler(
            batch_size=settings.dreaming_scheduler_batch_size,
            start_hour=settings.conversation_session_business_day_start_hour,
            node_id=daily_scan_node_id,
        )
        logger.info("dreaming scheduler started: %s", scheduler.status())

    @app.on_event("startup")
    async def startup_user_meta_scheduler() -> None:
        if not getattr(settings, "user_meta_scheduler_enabled", False):
            return
        scheduler = start_user_meta_scheduler(
            page_size=settings.user_meta_scheduler_page_size,
            inter_account_sleep=settings.user_meta_scheduler_inter_account_sleep,
            start_hour=settings.user_meta_scheduler_hour,
        )
        logger.info("user meta scheduler started: %s", scheduler.status())

    @app.on_event("shutdown")
    async def shutdown_proactive_scheduler() -> None:
        await stop_proactive_scheduler()

    @app.on_event("shutdown")
    async def shutdown_dreaming_scheduler() -> None:
        await stop_dreaming_scheduler()

    @app.on_event("shutdown")
    async def shutdown_user_meta_scheduler() -> None:
        await stop_user_meta_scheduler()
