"""共享进程生命周期：数据库、观测、事件循环与共享网关。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from app.bootstrap.runtime import set_background_loop
from app.config import settings
from app.db import close_pg_pool, init_db
from app.platform.gateways.openclaw import (
    close_persistent_gateway_client,
    warmup_persistent_gateway_client,
)
from app.platform.observability.alerting import configure_error_log_alerting
from app.time_utils import verify_host_timezone

logger = logging.getLogger("ai4all.bootstrap.lifecycle")


def install_shared_startup(app: FastAPI) -> None:
    """注册所有产品共用的启动事件，顺序稳定。"""

    @app.on_event("startup")
    def startup_platform() -> None:
        if settings.has_central_role:
            init_db()
        else:
            from app.db._core import connect

            with connect() as conn:
                conn.execute("SELECT 1")
        configure_error_log_alerting(settings)
        verify_host_timezone()

    @app.on_event("startup")
    async def capture_event_loop() -> None:
        set_background_loop(asyncio.get_running_loop())

    @app.on_event("startup")
    async def verify_tdai_multitenant() -> None:
        if not (
            getattr(settings, "tdai_enabled", False)
            and getattr(settings, "tdai_search_enabled", False)
        ):
            return
        from app.platform.search.tdai import mark_multitenant_unsafe, verify_multitenant

        ok = await asyncio.to_thread(verify_multitenant)
        if ok is False:
            logger.critical(
                "TDAI gateway 未处于 multiTenant 强隔离模式，已强制关闭 tdai search 工具。"
            )
            mark_multitenant_unsafe()
        elif ok is None:
            logger.warning(
                "TDAI multiTenant 探针未能确认，主动检索仍受显式开关和 allowlist gating。"
            )

    @app.on_event("startup")
    async def startup_persistent_gateway_client() -> None:
        if not getattr(settings, "openclaw_gateway_ws_warmup_on_startup", False):
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, warmup_persistent_gateway_client)
        except Exception as err:
            logger.warning("persistent OpenClaw Gateway warmup failed: %s", err)


def install_shared_shutdown(app: FastAPI) -> None:
    """注册应在产品 scheduler 关闭之后执行的共享关停事件。"""

    @app.on_event("shutdown")
    async def shutdown_persistent_gateway_client() -> None:
        try:
            close_persistent_gateway_client()
        except Exception as err:
            logger.warning("persistent OpenClaw Gateway close failed: %s", err)

    @app.on_event("shutdown")
    async def shutdown_db_pool() -> None:
        try:
            close_pg_pool()
        except Exception as err:
            logger.warning("PG connection pool close failed: %s", err)
