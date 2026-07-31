"""动态提醒（例行简报）履约层。

到期履约本质是「一次没有用户输入的合成对话轮次」：给定专用 system prompt + 一个可配
工具集，跑与普通对话同一套 tool loop，产出一条消息再发。工具集是策略参数而非写死单工具，
v1 默认 web_search + 首轮强制，后续放开只改配置。

见 docs/architecture/products/zhaoxi/dynamic_reminder_scheduled_content_design.md。
"""

from app.products.zhaoxi.proactive.fulfillment.dynamic_reminder import (
    FulfillmentResult,
    fulfill_dynamic_reminder,
    is_dynamic_reminder_allowed,
)

__all__ = [
    "FulfillmentResult",
    "fulfill_dynamic_reminder",
    "is_dynamic_reminder_allowed",
]
