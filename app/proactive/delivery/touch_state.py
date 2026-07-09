"""可触达性判断：微信对沉默联系人的送达窗口。

微信主动消息能否送达取决于会话凭证（context_token）是否新鲜，只由联系人自己的入站消息刷新；
服务端有效期是官方口径的 24 小时，超过后微信会静默拒收（无提示、我方无法绕过），详见
`docs/troubleshooting/weixin_context_token_send_semantics.md` 与决策记录
`docs/plans/主动消息送达窗口对齐.md`。24 小时是确认过的事实，不留缓冲——缓冲只会把本来能送达的
候选提前当作 stale 放弃，白白浪费机会。

`get_account_touch_state` 是判断入口，业务代码统一走这里，不要各处自己拼日期比较。
"""
from datetime import datetime
from typing import Literal, Optional

from app.db import get_account_last_inbound_at
from app.proactive.store.candidates import _parse_reactivation_time
from app.time_utils import beijing_naive_now

TouchState = Literal["reachable", "stale"]

REACHABLE: TouchState = "reachable"
STALE: TouchState = "stale"

WECHAT_TOUCH_WINDOW_HOURS = 24


def get_account_touch_state(
    *,
    account_id: str,
    now: Optional[datetime] = None,
) -> TouchState:
    """判断账号当前是否处于微信主动消息送达窗口内。

    没有任何入站记录（从未收到过消息）视为 stale，不应被当作可触达。
    """
    current = now or beijing_naive_now()
    if current.tzinfo is not None:
        # 部分调用方（如 admin run-once）传入 beijing_now() 的 tz-aware 值；本系统
        # DB 时间戳一律是北京 naive 裸串，这里统一去掉 tzinfo 才能和 last_inbound 相减。
        current = current.replace(tzinfo=None)
    last_inbound = _parse_reactivation_time(get_account_last_inbound_at(account_id=account_id))
    if last_inbound is None:
        return STALE
    elapsed_hours = (current - last_inbound).total_seconds() / 3600.0
    return STALE if elapsed_hours > WECHAT_TOUCH_WINDOW_HOURS else REACHABLE
