"""进程级运行时句柄。当前仅持有后台事件循环（startup 时由 main 写入），
供拆分后的 router 在请求时读取（call-time 读取，拿到的就是已就绪的 loop）。"""
import asyncio
from typing import Optional

_background_loop: Optional[asyncio.AbstractEventLoop] = None


def set_background_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """startup 钩子写入运行中的事件循环。"""
    global _background_loop
    _background_loop = loop


def get_background_loop() -> Optional[asyncio.AbstractEventLoop]:
    """返回后台事件循环（未就绪时为 None）。"""
    return _background_loop
