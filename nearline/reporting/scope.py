"""服务端日报作用域注册表。

日报以产品（app_id）和传输渠道（channel）为稳定隔离键；展示名只用于标题，
不能参与查询。新增产品/渠道时在这里显式注册，避免客户端输入任意作用域。
"""

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class ReportScope:
    """一份产品渠道日报的服务端可信作用域。"""

    app_id: str
    channel: str
    product_name: str
    channel_name: str
    onboarding_mode: str = "wechat_chat"

    @property
    def key(self) -> str:
        """返回可用于状态/文件名的稳定键。"""
        return f"{self.app_id}_{self.channel}"

    @property
    def title(self) -> str:
        """返回面向运营展示的作用域标题。"""
        return f"{self.product_name}（{self.channel_name}）"


_SCOPES: Dict[Tuple[str, str], ReportScope] = {
    ("zhaoxi", "openclaw-weixin"): ReportScope(
        app_id="zhaoxi",
        channel="openclaw-weixin",
        product_name="朝夕相伴",
        channel_name="微信渠道",
    ),
    # 已注册但不进入当前定时任务；App 正式运营时可直接以 CLI 参数启用。
    ("zhaoxi", "native"): ReportScope(
        app_id="zhaoxi",
        channel="native",
        product_name="朝夕相伴",
        channel_name="App 渠道",
        onboarding_mode="not_configured",
    ),
}


def resolve_scope(app_id: str, channel: str) -> ReportScope:
    """解析已注册的日报作用域；未知组合 fail closed。"""
    key = (str(app_id or "").strip(), str(channel or "").strip())
    try:
        return _SCOPES[key]
    except KeyError as err:
        raise ValueError(f"unregistered report scope: {key[0] or '<empty>'}/{key[1] or '<empty>'}") from err
