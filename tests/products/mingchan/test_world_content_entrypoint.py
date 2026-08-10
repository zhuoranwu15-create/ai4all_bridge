"""鸣蝉 world-content 进程入口能力闸门。"""

import asyncio
from unittest.mock import patch

from scripts.run_mingchan_world_content_scheduler import main


def test_mingchan_world_content_entrypoint_exits_while_feed_is_disabled(caplog):
    with patch(
        "scripts.run_mingchan_world_content_scheduler.settings.mingchan_feed_enabled",
        False,
    ):
        asyncio.run(main())
    assert "disabled by MINGCHAN_FEED_ENABLED=false" in caplog.text
