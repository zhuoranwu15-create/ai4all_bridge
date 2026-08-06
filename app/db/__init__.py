"""共享 DB 包与产品 persistence 的懒加载兼容 façade。"""
from importlib import import_module

from app.db._core import *  # noqa: F401,F403
from app.db.ops import *  # noqa: F401,F403
from app.db.analytics import *  # noqa: F401,F403
from app.db.admin import *  # noqa: F401,F403
from app.db.billing import *  # noqa: F401,F403
from app.db.product_memberships import *  # noqa: F401,F403
from app.db.accounts import *  # noqa: F401,F403
from app.db.runtime_ownerships import *  # noqa: F401,F403
from app.db.lifecycle import *  # noqa: F401,F403
from app.db.llm_config import *  # noqa: F401,F403


_COMPAT_EXPORT_MODULES = (
    "app.platform.gateways.persistence",
    "app.platform.moderation.persistence",
    "app.products.mingchan.infrastructure.persistence.companion_world",
    "app.products.mingchan.infrastructure.persistence.companion_world_lifecycle",
    "app.products.mingchan.infrastructure.persistence.companion_world_mailbox",
    "app.products.mingchan.infrastructure.persistence.companion_world_visits",
    "app.products.mingchan.infrastructure.persistence.companion_world_human_chat",
    "app.products.mingchan.infrastructure.persistence.notifications",
    "app.products.mingchan.infrastructure.persistence.resident_wishes",
    "app.products.zhaoxi.infrastructure.persistence.proactive",
    "app.products.zhaoxi.infrastructure.persistence.user_meta",
    "app.products.zhaoxi.infrastructure.persistence.mission",
    "app.products.zhaoxi.infrastructure.persistence.campaign",
    "app.products.zhaoxi.infrastructure.persistence.campaign_analytics",
)


def __getattr__(name: str):
    """按 owner 模块解析旧 ``from app.db import X`` 公共接口。"""

    for module_name in _COMPAT_EXPORT_MODULES:
        module = import_module(module_name)
        if name not in getattr(module, "__all__", ()):
            continue
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'app.db' has no attribute {name!r}")
