"""鸣蝉产品生命周期组合；产品禁用时不执行任何运行时动作。"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
)
from app.platform.media.access import validate_media_signing_config

logger = logging.getLogger("ai4all.products.mingchan.lifecycle")


def install_lifecycle(app: FastAPI) -> None:
    """注册鸣蝉启动校验；后台任务将在对应服务迁入后逐项接入。"""

    @app.on_event("startup")
    def validate_mingchan_runtime_config() -> None:
        try:
            PRODUCTION_PRODUCT_REGISTRY.require_enabled(MINGCHAN_APP_ID)
        except ValueError:
            logger.info("mingchan lifecycle skipped: product registry disabled")
            return
        # 鸣蝉媒体能力启用时必须具备不可预测的签名 secret。
        validate_media_signing_config()


__all__ = ["install_lifecycle"]
