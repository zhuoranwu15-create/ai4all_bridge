from datetime import timedelta

from app.time_utils import beijing_naive_now, beijing_now


from tests.factories import create_account as _create_account


from tests.factories import create_route as _create_route


def test_touch_state_stale_when_no_channel_bindings(client, fresh_db):
    from app.proactive.delivery.touch_state import STALE, get_account_touch_state

    _create_account("acc-touch-no-binding")
    assert get_account_touch_state(account_id="acc-touch-no-binding") == STALE


def test_touch_state_reachable_within_window(client, fresh_db):
    from app.proactive.delivery.touch_state import REACHABLE, get_account_touch_state

    _create_account("acc-touch-fresh")
    _create_route("acc-touch-fresh")
    now = beijing_naive_now() + timedelta(hours=5)
    assert get_account_touch_state(account_id="acc-touch-fresh", now=now) == REACHABLE


def test_touch_state_stale_beyond_window(client, fresh_db):
    from app.proactive.delivery.touch_state import STALE, get_account_touch_state

    _create_account("acc-touch-stale")
    _create_route("acc-touch-stale")
    now = beijing_naive_now() + timedelta(hours=25)
    assert get_account_touch_state(account_id="acc-touch-stale", now=now) == STALE


def test_touch_state_uses_most_recent_of_multiple_bindings(client, fresh_db):
    from app.proactive.delivery.touch_state import REACHABLE, get_account_touch_state

    _create_account("acc-touch-multi")
    _create_route("acc-touch-multi", session_key="session-a")
    _create_route("acc-touch-multi", session_key="session-b")
    now = beijing_naive_now() + timedelta(hours=5)
    assert get_account_touch_state(account_id="acc-touch-multi", now=now) == REACHABLE


def test_touch_state_accepts_tz_aware_now(client, fresh_db):
    """admin run-once 等调用方传入 beijing_now()（tz-aware），不应报错。"""
    from app.proactive.delivery.touch_state import STALE, get_account_touch_state

    _create_account("acc-touch-aware")
    _create_route("acc-touch-aware")
    now = beijing_now() + timedelta(hours=25)
    assert get_account_touch_state(account_id="acc-touch-aware", now=now) == STALE


def test_touch_state_reachable_just_under_24h(client, fresh_db):
    from app.proactive.delivery.touch_state import REACHABLE, get_account_touch_state

    _create_account("acc-touch-boundary-under")
    _create_route("acc-touch-boundary-under")
    now = beijing_naive_now() + timedelta(hours=23, minutes=59)
    assert get_account_touch_state(account_id="acc-touch-boundary-under", now=now) == REACHABLE


def test_touch_state_stale_just_over_24h(client, fresh_db):
    from app.proactive.delivery.touch_state import STALE, get_account_touch_state

    _create_account("acc-touch-boundary-over")
    _create_route("acc-touch-boundary-over")
    now = beijing_naive_now() + timedelta(hours=24, minutes=1)
    assert get_account_touch_state(account_id="acc-touch-boundary-over", now=now) == STALE
