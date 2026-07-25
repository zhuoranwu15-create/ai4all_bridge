"""朝夕旧 turn API 的兼容 façade；新代码使用产品入口或 Runtime API。"""

import sys

from app.products.zhaoxi.application.legacy_turn_service import (
    LEGACY_TURN_SERVICE_MODULE,
)

sys.modules[__name__] = LEGACY_TURN_SERVICE_MODULE
