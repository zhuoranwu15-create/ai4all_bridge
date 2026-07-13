"""get_account_last_inbound_at 渠道隔离 + 双定义收敛（§7.5）单测。"""

from app.db import (
    get_account_last_inbound_at,
    get_or_create_session,
    upsert_channel_binding,
)


def _ensure_account(account_id: str) -> None:
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"seed-{account_id}",
        business_day="2026-07-13",
    )


def test_last_inbound_scoped_to_weixin_ignores_web(fresh_db):
    account_id = "acc-lastinbound"
    _ensure_account(account_id)
    # 只挂一条 web 足迹（无微信 binding）。
    upsert_channel_binding(
        account_id=account_id,
        channel="web",
        session_key="web:acc-lastinbound",
        channel_account_id="platform-user-1",
        sender_id="platform-user-1",
        chat_id=None,
        raw_identity={"source": "test"},
    )
    # 微信口径下：该账号没有微信入站 → None（Web 活跃不污染微信可触达判断）。
    assert get_account_last_inbound_at(account_id=account_id, channel="openclaw-weixin") is None
    # web 口径下能查到（证明数据确实写进去了，只是被渠道过滤掉）。
    assert get_account_last_inbound_at(account_id=account_id, channel="web") is not None


def test_last_inbound_returns_weixin_binding_time(fresh_db):
    account_id = "acc-lastinbound-wx"
    _ensure_account(account_id)
    upsert_channel_binding(
        account_id=account_id,
        channel="openclaw-weixin",
        session_key="wx",
        channel_account_id="bot-1",
        sender_id="sender",
        chat_id="wx-user@im",
        raw_identity={"source": "test"},
    )
    assert get_account_last_inbound_at(account_id=account_id, channel="openclaw-weixin") is not None


def test_channel_param_is_mandatory(fresh_db):
    # channel 必传：收敛为单一渠道作用域定义后，不允许渠道无关调用。
    try:
        get_account_last_inbound_at(account_id="x")  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("channel 应为必传参数")
