"""Seam B（§3）：accounts.channel 不被 Web 改写守卫。

update_account_channel=False 时，已存在账号的 accounts.channel 不被本次会话创建改写；
默认 True 时保持历史行为（覆写为本次 channel）。覆盖 get_or_create_session 与
get_or_create_account_active_session 两条建 session 路径。
"""


def _channel(account_id: str) -> str:
    from app.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT channel FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return row["channel"]


def test_get_or_create_session_preserves_channel_when_flag_false(fresh_db):
    from app.db import get_or_create_session

    account_id = "acc-chan-guard-1"
    # 首建：微信账号，channel 落 openclaw-weixin。
    get_or_create_session(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c", session_key="__account_active__",
    )
    assert _channel(account_id) == "openclaw-weixin"

    # Web 首触，禁改渠道：accounts.channel 仍为 openclaw-weixin。
    get_or_create_session(
        account_id=account_id, channel="web", sender_id="s",
        sender_name=None, chat_id=None, session_key="__web_active__",
        update_account_channel=False,
    )
    assert _channel(account_id) == "openclaw-weixin"

    # 默认 True：历史行为，channel 被覆写为本次 web。
    get_or_create_session(
        account_id=account_id, channel="web", sender_id="s",
        sender_name=None, chat_id=None, session_key="__web_active__",
    )
    assert _channel(account_id) == "web"


def test_active_session_preserves_channel_when_flag_false(fresh_db):
    from app.db import get_or_create_account_active_session

    account_id = "acc-chan-guard-2"
    get_or_create_account_active_session(
        account_id=account_id, channel="openclaw-weixin", sender_id="s",
        sender_name=None, chat_id="c",
    )
    assert _channel(account_id) == "openclaw-weixin"

    get_or_create_account_active_session(
        account_id=account_id, channel="web", sender_id="s",
        sender_name=None, chat_id=None,
        active_session_key="__web_active__", update_account_channel=False,
    )
    assert _channel(account_id) == "openclaw-weixin"


def test_new_account_still_writes_channel_when_flag_false(fresh_db):
    """禁改开关只作用于 ON CONFLICT 更新分支：全新账号仍按 INSERT 写入 channel。"""
    from app.db import get_or_create_session

    account_id = "acc-chan-guard-3"
    get_or_create_session(
        account_id=account_id, channel="web", sender_id="s",
        sender_name=None, chat_id=None, session_key="__web_active__",
        update_account_channel=False,
    )
    assert _channel(account_id) == "web"
