"""旧提醒规则 import 路径的兼容 façade。"""

import sys

from app.products.zhaoxi.proactive.obligations import reminder_schedule as _implementation

sys.modules[__name__] = _implementation
