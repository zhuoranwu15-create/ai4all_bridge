"""鸣蝉 world-content 进程入口启用闸门。"""

import asyncio

from scripts.run_mingchan_world_content_scheduler import main


def test_mingchan_world_content_entrypoint_exits_while_product_is_disabled(caplog):
    asyncio.run(main())
    assert "disabled by product registry" in caplog.text
