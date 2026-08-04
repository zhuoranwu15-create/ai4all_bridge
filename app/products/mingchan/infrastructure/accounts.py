"""鸣蝉 runtime account persistence adapter。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.bootstrap.product_registry import (
    MINGCHAN_APP_ID,
    PRODUCTION_PRODUCT_REGISTRY,
    ProductRegistry,
)
from app.db._backend import Connection
from app.db.billing import (
    create_resident_runtime_account,
    insert_resident_runtime_account,
)


def insert_mingchan_resident_runtime_account(
    *,
    platform_user_id: str,
    display_name: str,
    conn: Connection,
    system_prompt: str = "",
    initial_channel: str = "native",
    soul_seed: Optional[str] = None,
    identity_seed: Optional[str] = None,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """在调用方事务内创建固定归属鸣蝉的居民 runtime account。"""

    return insert_resident_runtime_account(
        platform_user_id=platform_user_id,
        display_name=display_name,
        system_prompt=system_prompt,
        initial_channel=initial_channel,
        app_id=MINGCHAN_APP_ID,
        soul_seed=soul_seed,
        identity_seed=identity_seed,
        registry=registry,
        conn=conn,
    )


def create_mingchan_resident_runtime_account(
    *,
    universe_id: str,
    character_template_id: str,
    display_name: str,
    template_version: str = "v1",
    system_prompt: str = "",
    origin: str = "preset",
    initial_channel: str = "native",
    joined_at: Optional[str] = None,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """创建固定归属鸣蝉的居民账号和 World resident 映射。"""

    return create_resident_runtime_account(
        universe_id=universe_id,
        character_template_id=character_template_id,
        display_name=display_name,
        template_version=template_version,
        system_prompt=system_prompt,
        origin=origin,
        initial_channel=initial_channel,
        joined_at=joined_at,
        app_id=MINGCHAN_APP_ID,
        registry=registry,
    )


__all__ = [
    "create_mingchan_resident_runtime_account",
    "insert_mingchan_resident_runtime_account",
]
