"""平台 Bearer 鉴权比较 helper：恒定时间比较 + 空 token 永不通过。

刻意做成只依赖 stdlib `hmac` 的独立小模块：瘦接入节点 `node_agent`（设计上不拉起
中心 app/db 栈）也能复用，避免各处重复 `authorization == f"Bearer {token}"` 写法
带来的两类隐患——
  1) 空/未配置 token 时 `expected == "Bearer "`，发 `Authorization: Bearer ` 即放行；
  2) `==`/`!=` 短路比较暴露 token 前缀的时序侧信道。
"""
import hmac
from typing import Optional


def bearer_matches(configured_token: Optional[str], authorization: Optional[str]) -> bool:
    """当且仅当 `authorization` 恰为 `Bearer <configured_token>` 时返回 True。

    - 配置 token 为空或全空白 → 一律返回 False（空 token 永不通过，与 app_env 无关，
      作为生产 fail-fast 之外的纵深防线）。
    - 命中时用 `hmac.compare_digest` 恒定时间比较，消除按字节猜测 token 的时序侧信道。
    """
    token = str(configured_token or "").strip()
    if not token:
        return False
    return hmac.compare_digest(authorization or "", f"Bearer {token}")
